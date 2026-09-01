import hashlib
import json
from http import HTTPStatus

import httpx
import pytest
from nomad.utils import generate_entry_id

from sand.hysprint.sheet import DERIVED_SHEET_MAINFILE, EXTRACTED_JSON_MAINFILE
from sand.services.nomad_api import NomadAPIError, NomadAuthError, entry_ref
from sand.services.voice_eln import (
    EXPERIMENT_MAINFILE,
    DerivedSheet,
    SheetEditedError,
    VoiceElnService,
    normalize_audio_filename,
)

BASE_URL = 'http://localhost:8000/nomad-oasis/api/v1'
UPLOAD_ID = 'up-123'
SAND_COLLECTION_ID = generate_entry_id(UPLOAD_ID, EXPERIMENT_MAINFILE)
SHEET_ENTRY_ID = generate_entry_id(UPLOAD_ID, DERIVED_SHEET_MAINFILE)


def _service() -> VoiceElnService:
    # retry_interval_s=0: no sleeps in tests
    return VoiceElnService(BASE_URL, retry_interval_s=0, write_timeout_s=5)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_URL)


class _FakeNomad:
    """Programmable NOMAD API: upload status, raw files, and entry queries.

    `processing_polls` makes that many status GETs report process_running
    before the upload goes idle. `blocked_writes` rejects that many PUTs
    with NOMAD's processing-lock error (the check-then-PUT race).
    """

    def __init__(
        self,
        query_results=None,
        processing_polls=0,
        blocked_writes=0,
        published=False,
        upload_exists=True,
    ):
        self.raw_files: dict[str, bytes] = {}
        self.query_results = query_results or []
        self.processing_polls = processing_polls
        self.blocked_writes = blocked_writes
        self.published = published
        self.upload_exists = upload_exists
        self.put_attempts = 0
        # entry_id -> list of data sections; each GET pops one (last sticks),
        # so a test can serve different processed_archive before/after a write
        self.entry_archives: dict[str, list[dict]] = {}
        self.deleted: list[str] = []

    def _status(self) -> httpx.Response:
        if not self.upload_exists:
            return httpx.Response(404, json={'detail': 'upload not found'})
        running = self.processing_polls > 0
        if running:
            self.processing_polls -= 1
        return httpx.Response(
            200,
            json={'data': {'process_running': running, 'published': self.published}},
        )

    def _put_raw(self, request: httpx.Request) -> httpx.Response:
        self.put_attempts += 1
        if self.blocked_writes > 0:
            self.blocked_writes -= 1
            # after a blocked PUT the service re-checks the status; report
            # processing once so it retries instead of failing
            self.processing_polls = max(self.processing_polls, 1)
            return httpx.Response(
                400,
                json={'detail': 'The upload is currently blocked by another process.'},
            )
        name = request.url.params['file_name']
        if '/' in name:
            return httpx.Response(400, json={'detail': 'Bad file name provided.'})
        directory = request.url.path.split('/raw/', 1)[1].strip('/')
        full_name = f'{directory}/{name}' if directory else name
        self.raw_files[full_name] = request.content
        return httpx.Response(200, json={})

    def _raw(self, request: httpx.Request) -> httpx.Response:
        if request.method == 'PUT':
            return self._put_raw(request)
        name = request.url.path.split('/raw/', 1)[1]
        if name not in self.raw_files:
            return httpx.Response(404, json={'detail': 'not found'})
        if request.method == 'DELETE':
            del self.raw_files[name]
            self.deleted.append(name)
            return httpx.Response(200, json={})
        return httpx.Response(200, content=self.raw_files[name])

    def _entry_archive(self, entry_id: str) -> httpx.Response:
        sections = self.entry_archives.get(entry_id)
        if not sections:
            return httpx.Response(404, json={'detail': 'not found'})
        data = sections.pop(0) if len(sections) > 1 else sections[0]
        return httpx.Response(200, json={'data': {'archive': {'data': data}}})

    def _entries(self, request: httpx.Request, path: str) -> httpx.Response:
        if request.method == 'GET' and path.endswith('/archive'):
            return self._entry_archive(
                path.split('/entries/')[1].split('/', maxsplit=1)[0]
            )
        if request.method == 'POST' and path.endswith('/entries/query'):
            return httpx.Response(200, json={'data': self.query_results})
        return httpx.Response(404, json={'detail': f'unexpected {path}'})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == 'POST' and path.endswith('/uploads'):
            return httpx.Response(200, json={'upload_id': UPLOAD_ID})
        if '/entries/' in path:
            return self._entries(request, path)
        if f'/uploads/{UPLOAD_ID}/raw/' in path:
            return self._raw(request)
        if request.method == 'GET' and '/uploads/' in path:
            if path.endswith('/uploads/' + UPLOAD_ID):
                return self._status()
            return httpx.Response(404, json={'detail': 'upload not found'})
        return httpx.Response(404, json={'detail': f'unexpected {path}'})

    def archive(self, mainfile: str) -> dict:
        return json.loads(self.raw_files[mainfile])


INFO = {
    'project_name': 'perov',
    'batch': 'B1',
    'subbatch': 'a',
    'first_sample': '1',
    'n_samples': 4,
}


@pytest.mark.asyncio
async def test_create_experiment_writes_collection_and_info_note():
    fake = _FakeNomad()

    async with _client(fake) as client:
        service = _service()
        result = await service.create_input_collection(client, 'perov_B1_a')
        await service.add_experiment_info(
            client, UPLOAD_ID, json.dumps(INFO), collection_entry_id=SAND_COLLECTION_ID
        )

    assert result.upload_id == UPLOAD_ID
    assert result.entry_id == generate_entry_id(UPLOAD_ID, EXPERIMENT_MAINFILE)

    info_note = fake.archive('experiment_info.archive.json')['data']
    assert info_note['label'] == 'experiment_info'
    assert json.loads(info_note['text']) == INFO
    assert info_note['datetime']

    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert collection['name'] == 'perov_B1_a'
    info_entry_id = generate_entry_id(UPLOAD_ID, 'experiment_info.archive.json')
    assert collection['notes'] == [entry_ref(UPLOAD_ID, info_entry_id)]


@pytest.mark.asyncio
async def test_create_experiment_without_info_has_no_notes():
    fake = _FakeNomad()

    async with _client(fake) as client:
        await _service().create_input_collection(client, 'scratch')

    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert 'notes' not in collection
    assert list(fake.raw_files) == [EXPERIMENT_MAINFILE]


@pytest.mark.asyncio
async def test_add_audio_stores_file_and_references_it_from_collection():
    fake = _FakeNomad()

    async with _client(fake) as client:
        service = _service()
        await service.create_input_collection(client, 'perov_B1_a')
        result = await service.add_audio(
            client,
            UPLOAD_ID,
            b'AUDIO',
            'rec.m4a',
            collection_entry_id=SAND_COLLECTION_ID,
        )

    audio_files = [n for n in fake.raw_files if n.endswith('_rec.m4a')]
    assert len(audio_files) == 1
    assert fake.raw_files[audio_files[0]] == b'AUDIO'
    # the entry id matches the parser's deterministic companion mainfile
    assert result.entry_id == generate_entry_id(
        UPLOAD_ID, f'{audio_files[0]}.archive.json'
    )
    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert collection['audios'] == [entry_ref(UPLOAD_ID, result.entry_id)]


@pytest.mark.asyncio
async def test_add_audio_without_collection_stores_no_file():
    # fail before storing the audio: an orphaned file could never be
    # referenced, and retries would deposit more copies
    fake = _FakeNomad()

    async with _client(fake) as client:
        with pytest.raises(NomadAPIError) as excinfo:
            await _service().add_audio(
                client,
                UPLOAD_ID,
                b'AUDIO',
                'rec.m4a',
                collection_entry_id=SAND_COLLECTION_ID,
            )

    assert excinfo.value.status_code == HTTPStatus.NOT_FOUND
    assert fake.raw_files == {}


@pytest.mark.asyncio
async def test_add_note_writes_step_note_and_references_it():
    fake = _FakeNomad()

    async with _client(fake) as client:
        service = _service()
        await service.create_input_collection(client, 'perov_B1_a')
        result = await service.add_written_note(
            client,
            UPLOAD_ID,
            'spun coat at 2000 rpm',
            collection_entry_id=SAND_COLLECTION_ID,
        )

    note_files = [n for n in fake.raw_files if n.startswith('note_')]
    assert len(note_files) == 1
    note = fake.archive(note_files[0])['data']
    assert note['text'] == 'spun coat at 2000 rpm'
    assert note['label'] == 'step'
    assert result.entry_id == generate_entry_id(UPLOAD_ID, note_files[0])
    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert collection['notes'] == [entry_ref(UPLOAD_ID, result.entry_id)]


@pytest.mark.asyncio
async def test_append_writes_back_nested_mainfile():
    # PUT raw takes the directory in the URL and a bare basename in
    # file_name; a nested mainfile must be split, not passed verbatim
    fake = _FakeNomad(query_results=[{'mainfile': 'exp/my_collection.archive.json'}])
    fake.raw_files['exp/my_collection.archive.json'] = json.dumps(
        {'data': {'m_def': 'x', 'name': 'manual'}}
    ).encode()

    async with _client(fake) as client:
        result = await _service().add_written_note(
            client, UPLOAD_ID, 'a step', collection_entry_id='e-nested'
        )

    collection = fake.archive('exp/my_collection.archive.json')['data']
    assert collection['notes'] == [entry_ref(UPLOAD_ID, result.entry_id)]


@pytest.mark.asyncio
async def test_append_handles_null_refs_field():
    # NOMAD deserializes explicit nulls (its "unset" value); appending to
    # a collection with '"notes": null' must not crash with a TypeError
    fake = _FakeNomad()
    fake.raw_files[EXPERIMENT_MAINFILE] = json.dumps(
        {'data': {'m_def': 'x', 'name': 'n', 'notes': None}}
    ).encode()

    async with _client(fake) as client:
        result = await _service().add_written_note(
            client, UPLOAD_ID, 'a step', collection_entry_id=SAND_COLLECTION_ID
        )

    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert collection['notes'] == [entry_ref(UPLOAD_ID, result.entry_id)]


@pytest.mark.asyncio
async def test_append_rejects_collection_without_data_section():
    fake = _FakeNomad()
    fake.raw_files[EXPERIMENT_MAINFILE] = json.dumps({'data': [1, 2]}).encode()

    async with _client(fake) as client:
        with pytest.raises(NomadAPIError, match='no data section'):
            await _service().add_written_note(
                client, UPLOAD_ID, 'a step', collection_entry_id=SAND_COLLECTION_ID
            )


@pytest.mark.asyncio
async def test_list_input_collections_returns_summaries():
    fake = _FakeNomad(
        query_results=[
            {
                'upload_id': UPLOAD_ID,
                'entry_id': 'e-1',
                'entry_name': 'perov_B1_a',
            }
        ]
    )

    async with _client(fake) as client:
        experiments = await _service().list_input_collections(client)

    assert len(experiments) == 1
    assert experiments[0].upload_id == UPLOAD_ID
    assert experiments[0].entry_id == 'e-1'
    assert experiments[0].name == 'perov_B1_a'


@pytest.mark.asyncio
async def test_write_waits_for_processing_and_sends_body_once():
    # the first PUT triggers processing; the service polls the upload's
    # process_running state and PUTs each file exactly once (no re-sending
    # the body against NOMAD's processing lock)
    fake = _FakeNomad(processing_polls=2)

    async with _client(fake) as client:
        service = _service()
        result = await service.create_input_collection(client, 'perov_B1_a')
        await service.add_experiment_info(
            client, UPLOAD_ID, json.dumps(INFO), collection_entry_id=SAND_COLLECTION_ID
        )

    assert result.upload_id == UPLOAD_ID
    assert 'experiment_info.archive.json' in fake.raw_files
    assert EXPERIMENT_MAINFILE in fake.raw_files
    files_written = 3  # collection, note, collection append
    assert fake.put_attempts == files_written


@pytest.mark.asyncio
async def test_write_retries_when_processing_starts_after_the_idle_check():
    # the check-then-PUT race: NOMAD rejects the PUT although the upload
    # looked idle; the service re-checks the status and retries
    fake = _FakeNomad(blocked_writes=1)

    async with _client(fake) as client:
        await _service().create_input_collection(client, 'x')

    assert EXPERIMENT_MAINFILE in fake.raw_files


@pytest.mark.asyncio
async def test_write_to_unknown_upload_raises_not_found():
    fake = _FakeNomad(upload_exists=False)

    async with _client(fake) as client:
        with pytest.raises(NomadAPIError) as excinfo:
            await _service().add_written_note(
                client, UPLOAD_ID, 'a step', collection_entry_id=SAND_COLLECTION_ID
            )

    assert excinfo.value.status_code == HTTPStatus.NOT_FOUND
    assert fake.raw_files == {}


@pytest.mark.asyncio
async def test_collection_entry_id_resolves_sand_mainfile_without_index():
    # sand's own experiments resolve deterministically, so adding to a
    # just-created (not yet indexed) experiment works with an entry id
    fake = _FakeNomad()

    async with _client(fake) as client:
        service = _service()
        created = await service.create_input_collection(client, 'perov_B1_a')
        result = await service.add_written_note(
            client, UPLOAD_ID, 'a step', collection_entry_id=created.entry_id
        )

    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert collection['notes'] == [entry_ref(UPLOAD_ID, result.entry_id)]


@pytest.mark.asyncio
async def test_collection_entry_id_resolves_foreign_mainfile_by_query():
    # two collections in one upload: the entry id pins the chosen one
    # instead of falling back to the oldest
    fake = _FakeNomad(query_results=[{'mainfile': 'second.archive.json'}])
    fake.raw_files['second.archive.json'] = json.dumps(
        {'data': {'m_def': 'x', 'name': 'second'}}
    ).encode()

    async with _client(fake) as client:
        result = await _service().add_written_note(
            client, UPLOAD_ID, 'a step', collection_entry_id='e-second'
        )

    collection = fake.archive('second.archive.json')['data']
    assert collection['notes'] == [entry_ref(UPLOAD_ID, result.entry_id)]


@pytest.mark.asyncio
async def test_unknown_collection_entry_id_raises_not_found():
    fake = _FakeNomad(query_results=[])

    async with _client(fake) as client:
        with pytest.raises(NomadAPIError) as excinfo:
            await _service().add_audio(
                client, UPLOAD_ID, b'AUDIO', 'rec.m4a', collection_entry_id='e-gone'
            )

    assert excinfo.value.status_code == HTTPStatus.NOT_FOUND
    assert fake.raw_files == {}


@pytest.mark.asyncio
async def test_write_to_published_upload_is_rejected():
    fake = _FakeNomad(published=True)

    async with _client(fake) as client:
        with pytest.raises(NomadAPIError, match='published'):
            await _service().add_written_note(
                client, UPLOAD_ID, 'a step', collection_entry_id=SAND_COLLECTION_ID
            )

    assert fake.raw_files == {}


# --- derived sheet: extraction file, edited-sheet guard, orphan cleanup ---


def _extraction_for(xlsx: bytes) -> dict:
    # archive tied to the xlsx bytes, so different sheets mean different
    # archives (as in reality) and the no-op short-circuit does not trigger
    return {
        'archive': {'content': xlsx.decode()},
        'xlsx_sha256': hashlib.sha256(xlsx).hexdigest(),
    }


async def _add_sheet(service, client, xlsx: bytes, force=False):
    sheet = DerivedSheet(
        xlsx=xlsx,
        xlsx_mainfile=DERIVED_SHEET_MAINFILE,
        extraction=_extraction_for(xlsx),
        extraction_mainfile=EXTRACTED_JSON_MAINFILE,
    )
    return await service.add_derived_sheet(
        client,
        UPLOAD_ID,
        sheet,
        collection_entry_id=SAND_COLLECTION_ID,
        force=force,
    )


@pytest.mark.asyncio
async def test_add_derived_sheet_stores_extraction_file_and_links_parsed_entries():
    fake = _FakeNomad()
    fake.entry_archives[SHEET_ENTRY_ID] = [
        {
            'processed_archive': [
                entry_ref(UPLOAD_ID, 'p1'),
                entry_ref(UPLOAD_ID, 'p2'),
            ]
        }
    ]

    async with _client(fake) as client:
        service = _service()
        await service.create_input_collection(client, 'perov_B1_a')
        result = await _add_sheet(service, client, b'XLSX')

    assert fake.raw_files[DERIVED_SHEET_MAINFILE] == b'XLSX'
    extraction = fake.archive(EXTRACTED_JSON_MAINFILE)
    assert extraction['xlsx_sha256'] == hashlib.sha256(b'XLSX').hexdigest()
    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert collection['derived_entries'] == [
        entry_ref(UPLOAD_ID, result.entry_id),
        entry_ref(UPLOAD_ID, 'p1'),
        entry_ref(UPLOAD_ID, 'p2'),
    ]


@pytest.mark.asyncio
async def test_regenerate_refuses_to_overwrite_a_hand_edited_sheet():
    fake = _FakeNomad()

    async with _client(fake) as client:
        service = _service()
        await service.create_input_collection(client, 'perov_B1_a')
        fake.raw_files[DERIVED_SHEET_MAINFILE] = b'EDITED'
        fake.raw_files[EXTRACTED_JSON_MAINFILE] = json.dumps(
            _extraction_for(b'ORIGINAL')
        ).encode()
        with pytest.raises(SheetEditedError):
            await _add_sheet(service, client, b'NEW')
        # the edited sheet survives untouched
        assert fake.raw_files[DERIVED_SHEET_MAINFILE] == b'EDITED'

        await _add_sheet(service, client, b'NEW', force=True)

    assert fake.raw_files[DERIVED_SHEET_MAINFILE] == b'NEW'
    assert fake.archive(EXTRACTED_JSON_MAINFILE)['xlsx_sha256'] == (
        hashlib.sha256(b'NEW').hexdigest()
    )


@pytest.mark.asyncio
async def test_regenerate_over_unedited_or_pre_extraction_sheet_needs_no_force():
    fake = _FakeNomad()

    async with _client(fake) as client:
        service = _service()
        await service.create_input_collection(client, 'perov_B1_a')
        # no extraction file (pre-extraction upload): provenance unknown
        fake.raw_files[DERIVED_SHEET_MAINFILE] = b'LEGACY'
        await _add_sheet(service, client, b'NEW')

        # hash matches the extraction file: not edited
        await _add_sheet(service, client, b'NEWER')

    assert fake.raw_files[DERIVED_SHEET_MAINFILE] == b'NEWER'


@pytest.mark.asyncio
async def test_regenerate_with_identical_archive_skips_the_reparse():
    # new xlsx BYTES always differ (openpyxl is not byte-deterministic);
    # equality is judged on the archive in the extraction file
    fake = _FakeNomad()
    fake.entry_archives[SHEET_ENTRY_ID] = [
        {'processed_archive': [entry_ref(UPLOAD_ID, 'p1')]}
    ]

    async with _client(fake) as client:
        service = _service()
        await service.create_input_collection(client, 'perov_B1_a')
        fake.raw_files[DERIVED_SHEET_MAINFILE] = b'OLD'
        fake.raw_files[EXTRACTED_JSON_MAINFILE] = json.dumps(
            _extraction_for(b'OLD')
        ).encode()
        sheet = DerivedSheet(
            xlsx=b'DIFFERENT-BYTES',
            xlsx_mainfile=DERIVED_SHEET_MAINFILE,
            extraction={
                'archive': {'content': 'OLD'},  # same archive as stored
                'xlsx_sha256': hashlib.sha256(b'DIFFERENT-BYTES').hexdigest(),
            },
            extraction_mainfile=EXTRACTED_JSON_MAINFILE,
        )
        result = await service.add_derived_sheet(
            client, UPLOAD_ID, sheet, collection_entry_id=SAND_COLLECTION_ID
        )

    assert fake.raw_files[DERIVED_SHEET_MAINFILE] == b'OLD'  # untouched
    assert fake.deleted == []
    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert collection['derived_entries'] == [
        entry_ref(UPLOAD_ID, result.entry_id),
        entry_ref(UPLOAD_ID, 'p1'),
    ]


@pytest.mark.asyncio
async def test_regenerate_deletes_the_previous_parse_output_before_reparse():
    # the hysprint parser never overwrites an existing generated file, so
    # ALL old parse output must go before the re-parse - including entries
    # the new parse recreates under the same name (else stale content)
    fake = _FakeNomad(
        query_results=[
            {'entry_id': 'p-gone', 'mainfile': 'gone_sample.archive.json'},
            {'entry_id': 'p-kept', 'mainfile': 'kept_sample.archive.json'},
        ],
    )
    fake.entry_archives[SHEET_ENTRY_ID] = [
        {
            'processed_archive': [
                entry_ref(UPLOAD_ID, 'p-gone'),
                entry_ref(UPLOAD_ID, 'p-kept'),
            ]
        },
        {
            'processed_archive': [
                entry_ref(UPLOAD_ID, 'p-kept'),
                entry_ref(UPLOAD_ID, 'p-new'),
            ]
        },
    ]

    async with _client(fake) as client:
        service = _service()
        await service.create_input_collection(client, 'perov_B1_a')
        fake.raw_files[DERIVED_SHEET_MAINFILE] = b'OLD'
        fake.raw_files[EXTRACTED_JSON_MAINFILE] = json.dumps(
            _extraction_for(b'OLD')
        ).encode()
        fake.raw_files['gone_sample.archive.json'] = b'{}'
        fake.raw_files['kept_sample.archive.json'] = b'{}'
        result = await _add_sheet(service, client, b'NEW')

    assert sorted(fake.deleted) == [
        'gone_sample.archive.json',
        'kept_sample.archive.json',
    ]
    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert collection['derived_entries'] == [
        entry_ref(UPLOAD_ID, result.entry_id),
        entry_ref(UPLOAD_ID, 'p-kept'),
        entry_ref(UPLOAD_ID, 'p-new'),
    ]


@pytest.mark.asyncio
async def test_invalid_token_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={'detail': 'unauthorized'})

    async with _client(handler) as client:
        with pytest.raises(NomadAuthError):
            await _service().create_input_collection(client, 'x')


def test_build_client_sends_bearer_token():
    client = _service().build_client('tok-1')
    assert client.headers['Authorization'] == 'Bearer tok-1'


def test_normalize_audio_filename():
    # Safari's MediaRecorder output is MPEG-4 audio: store it as .m4a so
    # the voice-eln parser matches it
    assert normalize_audio_filename('recording.mp4') == 'recording.m4a'
    assert normalize_audio_filename('REC.M4A') == 'REC.m4a'
    assert normalize_audio_filename('a.webm') == 'a.webm'
    assert normalize_audio_filename('notes.txt') is None
    assert normalize_audio_filename('no_extension') is None
