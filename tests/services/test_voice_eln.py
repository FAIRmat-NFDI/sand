import asyncio
import json
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus

import httpx
import pytest
from nomad.utils import generate_entry_id

from sand.hysprint import EXPERIMENT_INFO_LABEL, EXPERIMENT_INFO_MAINFILE
from sand.services.nomad_api import NomadAPIError, NomadAuthError, entry_ref
from sand.services.voice_eln import (
    AUDIO_INPUT_M_DEF,
    EXPERIMENT_MAINFILE,
    WRITTEN_NOTE_M_DEF,
    AudioUpload,
    VoiceElnService,
    normalize_audio_filename,
)

BASE_URL = 'http://localhost:8000/nomad-oasis/api/v1'
UPLOAD_ID = 'up-123'
SAND_COLLECTION_ID = generate_entry_id(UPLOAD_ID, EXPERIMENT_MAINFILE)


def _service() -> VoiceElnService:
    # retry_interval_s=0: no sleeps in tests
    return VoiceElnService(BASE_URL, retry_interval_s=0, write_timeout_s=5)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_URL)


def _multipart_files(request: httpx.Request) -> dict[str, bytes]:
    """File name -> content of a multipart/form-data request body."""
    head = f'Content-Type: {request.headers["content-type"]}\r\n\r\n'.encode()
    message = BytesParser(policy=default).parsebytes(head + request.content)
    return {
        part.get_filename(): part.get_payload(decode=True)
        for part in message.iter_parts()
    }


class _FakeNomad:
    """Programmable NOMAD API: upload status, raw files, and entry queries.

    `entries` maps entry ids to mainfiles: an entry-id query finds its
    mainfile and the entry's archive is that raw file, as if processed.

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
        self.entries: dict[str, str] = {}
        self.query_results = query_results or []
        self.processing_polls = processing_polls
        self.blocked_writes = blocked_writes
        self.published = published
        self.upload_exists = upload_exists
        self.put_attempts = 0
        # file names of each accepted PUT, to see which files arrived together
        self.put_files: list[list[str]] = []

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
        directory = request.url.path.split('/raw/', 1)[1].strip('/')
        if 'file_name' in request.url.params:
            uploaded = {request.url.params['file_name']: request.content}
        else:
            uploaded = _multipart_files(request)
        for name, content in uploaded.items():
            if '/' in name:
                return httpx.Response(400, json={'detail': 'Bad file name provided.'})
            full_name = f'{directory}/{name}' if directory else name
            self.raw_files[full_name] = content
        self.put_files.append(sorted(uploaded))
        return httpx.Response(200, json={})

    def _raw(self, request: httpx.Request) -> httpx.Response:
        if request.method == 'PUT':
            return self._put_raw(request)
        name = request.url.path.split('/raw/', 1)[1]
        if name not in self.raw_files:
            return httpx.Response(404, json={'detail': 'not found'})
        return httpx.Response(200, content=self.raw_files[name])

    def _entries(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == 'POST' and path.endswith('/entries/query'):
            entry_id = json.loads(request.content)['query'].get('entry_id')
            if not (self.entries and entry_id):
                return httpx.Response(200, json={'data': self.query_results})
            found = self.entries.get(entry_id)
            data = [{'mainfile': found}] if found else []
            return httpx.Response(200, json={'data': data})
        mainfile = self.entries.get(path.split('/entries/')[1].split('/')[0])
        if request.method != 'GET' or mainfile not in self.raw_files:
            return httpx.Response(404, json={'detail': f'unexpected {path}'})
        archive = json.loads(self.raw_files[mainfile])
        return httpx.Response(200, json={'data': {'archive': archive}})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == 'POST' and path.endswith('/uploads'):
            return httpx.Response(200, json={'upload_id': UPLOAD_ID})
        if f'/uploads/{UPLOAD_ID}/raw/' in path:
            return self._raw(request)
        if request.method == 'GET' and '/uploads/' in path:
            if path.endswith('/uploads/' + UPLOAD_ID):
                return self._status()
            return httpx.Response(404, json={'detail': 'upload not found'})
        if '/entries/' in path:
            return self._entries(request)
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
        await service.add_written_note(
            client,
            UPLOAD_ID,
            json.dumps(INFO),
            collection_entry_id=SAND_COLLECTION_ID,
            label=EXPERIMENT_INFO_LABEL,
            mainfile=EXPERIMENT_INFO_MAINFILE,
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
async def test_add_audio_uploads_companion_and_audio_together():
    fake = _FakeNomad()

    async with _client(fake) as client:
        service = _service()
        await service.create_input_collection(client, 'perov_B1_a')
        result = await service.add_audio(
            client,
            UPLOAD_ID,
            AudioUpload(audio=b'AUDIO', filename='rec.m4a', label='cleaning'),
            collection_entry_id=SAND_COLLECTION_ID,
        )

    audio_files = [n for n in fake.raw_files if n.endswith('_rec.m4a')]
    assert len(audio_files) == 1
    assert fake.raw_files[audio_files[0]] == b'AUDIO'
    companion_file = f'{audio_files[0]}.archive.json'
    # one PUT, so NOMAD processes both in the same run: the parser skips the
    # existing companion and transcription starts with the audio present
    assert sorted([audio_files[0], companion_file]) in fake.put_files
    companion = fake.archive(companion_file)['data']
    assert companion['raw_audio'] == audio_files[0]
    assert companion['label'] == 'cleaning'
    assert companion['datetime']
    # no transcript: the voice-eln normalizer starts the transcription
    assert 'transcript' not in companion
    assert 'transcription_status' not in companion
    # the entry id is the companion's
    assert result.entry_id == generate_entry_id(
        UPLOAD_ID, f'{audio_files[0]}.archive.json'
    )
    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert collection['audios'] == [entry_ref(UPLOAD_ID, result.entry_id)]


@pytest.mark.asyncio
async def test_add_audio_with_transcript_writes_pretranscribed_companion():
    # a present transcript keeps the voice-eln normalizer from starting
    # the automatic (paid) transcription
    fake = _FakeNomad()

    async with _client(fake) as client:
        service = _service()
        await service.create_input_collection(client, 'perov_B1_a')
        result = await service.add_audio(
            client,
            UPLOAD_ID,
            AudioUpload(
                audio=b'AUDIO',
                filename='rec.m4a',
                transcript='UV ozone clean for all samples',
                stt_model='deepgram/nova-3',
                label='cleaning',
            ),
            collection_entry_id=SAND_COLLECTION_ID,
        )

    audio_files = [n for n in fake.raw_files if n.endswith('_rec.m4a')]
    assert len(audio_files) == 1
    companion = fake.archive(f'{audio_files[0]}.archive.json')['data']
    assert companion['raw_audio'] == audio_files[0]
    assert companion['transcript'] == 'UV ozone clean for all samples'
    assert companion['transcription_status'] == 'COMPLETED'
    assert companion['transcription_meta']['stt_model'] == 'deepgram/nova-3'
    assert companion['transcription_meta']['transcribed_at']
    assert companion['label'] == 'cleaning'
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
                AudioUpload(audio=b'AUDIO', filename='rec.m4a'),
                collection_entry_id=SAND_COLLECTION_ID,
            )

    assert excinfo.value.status_code == HTTPStatus.NOT_FOUND
    assert fake.raw_files == {}


@pytest.mark.asyncio
async def test_add_note_writes_labeled_note_and_references_it():
    fake = _FakeNomad()

    async with _client(fake) as client:
        service = _service()
        await service.create_input_collection(client, 'perov_B1_a')
        result = await service.add_written_note(
            client,
            UPLOAD_ID,
            'spun coat at 2000 rpm',
            collection_entry_id=SAND_COLLECTION_ID,
            label='spin coating',
        )

    note_files = [n for n in fake.raw_files if n.startswith('note_')]
    assert len(note_files) == 1
    note = fake.archive(note_files[0])['data']
    assert note['text'] == 'spun coat at 2000 rpm'
    assert note['label'] == 'spin coating'
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
        await service.add_written_note(
            client,
            UPLOAD_ID,
            json.dumps(INFO),
            collection_entry_id=SAND_COLLECTION_ID,
            label=EXPERIMENT_INFO_LABEL,
            mainfile=EXPERIMENT_INFO_MAINFILE,
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
                client,
                UPLOAD_ID,
                AudioUpload(audio=b'AUDIO', filename='rec.m4a'),
                collection_entry_id='e-gone',
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


def _fake_with_inputs(audio: dict, note: dict) -> _FakeNomad:
    """An experiment whose collection references one audio and one note;
    entry 'x1' exists in the upload but is not an input of it."""
    fake = _FakeNomad()
    fake.raw_files[EXPERIMENT_MAINFILE] = json.dumps(
        {
            'data': {
                'audios': [entry_ref(UPLOAD_ID, 'a1')],
                'notes': [entry_ref(UPLOAD_ID, 'n1')],
            }
        }
    ).encode()
    for entry_id, data in (
        ('a1', {'m_def': AUDIO_INPUT_M_DEF, **audio}),
        ('n1', {'m_def': WRITTEN_NOTE_M_DEF, **note}),
        ('x1', {'m_def': WRITTEN_NOTE_M_DEF, 'text': 'elsewhere'}),
    ):
        fake.entries[entry_id] = f'{entry_id}.archive.json'
        fake.raw_files[f'{entry_id}.archive.json'] = json.dumps({'data': data}).encode()
    return fake


@pytest.mark.asyncio
async def test_collect_inputs_takes_the_most_human_text_and_orders_by_time():
    fake = _fake_with_inputs(
        audio={
            'transcript': 'machine',
            'corrected_transcript': 'said',
            'intended_transcript': 'meant',
            'transcription_status': 'COMPLETED',
            'datetime': '2026-09-22T10:00:00+00:00',
        },
        note={'text': 'a note', 'datetime': '2026-09-22T09:00:00+00:00'},
    )

    async with _client(fake) as client:
        inputs = await _service().collect_inputs(client, UPLOAD_ID, SAND_COLLECTION_ID)

    assert [i.entry_id for i in inputs] == ['n1', 'a1']
    audio = inputs[1]
    assert (audio.text, audio.corrected, audio.status) == ('meant', True, 'COMPLETED')
    assert inputs[0].status is None


@pytest.mark.asyncio
async def test_collect_inputs_reports_untranscribed_audio_without_text():
    fake = _fake_with_inputs(
        audio={'transcription_status': 'PENDING'}, note={'text': 'a note'}
    )

    async with _client(fake) as client:
        inputs = await _service().collect_inputs(client, UPLOAD_ID, SAND_COLLECTION_ID)

    audio = next(i for i in inputs if i.kind == 'audio')
    assert (audio.text, audio.corrected, audio.status) == (None, False, 'PENDING')


@pytest.mark.asyncio
async def test_revise_audio_sets_and_withdraws_the_correction():
    fake = _fake_with_inputs(audio={'transcript': 'machine'}, note={'text': 'n'})

    async with _client(fake) as client:
        service = _service()
        await service.revise_input(
            client, UPLOAD_ID, 'a1', 'fixed', collection_entry_id=SAND_COLLECTION_ID
        )
        assert fake.archive('a1.archive.json')['data']['corrected_transcript'] == (
            'fixed'
        )
        await service.revise_input(
            client, UPLOAD_ID, 'a1', '  ', collection_entry_id=SAND_COLLECTION_ID
        )

    audio = fake.archive('a1.archive.json')['data']
    assert 'corrected_transcript' not in audio
    assert audio['transcript'] == 'machine'


@pytest.mark.asyncio
async def test_revise_note_rejects_empty_text():
    fake = _fake_with_inputs(audio={}, note={'text': 'keep me'})

    async with _client(fake) as client:
        with pytest.raises(ValueError):
            await _service().revise_input(
                client, UPLOAD_ID, 'n1', ' ', collection_entry_id=SAND_COLLECTION_ID
            )

    assert fake.archive('n1.archive.json')['data']['text'] == 'keep me'


@pytest.mark.asyncio
async def test_revise_rejects_an_entry_outside_the_collection():
    fake = _fake_with_inputs(audio={}, note={'text': 'n'})
    puts_before = fake.put_attempts

    async with _client(fake) as client:
        with pytest.raises(NomadAPIError) as excinfo:
            await _service().revise_input(
                client,
                UPLOAD_ID,
                'x1',
                'hijack',
                collection_entry_id=SAND_COLLECTION_ID,
            )

    assert excinfo.value.status_code == HTTPStatus.NOT_FOUND
    assert fake.put_attempts == puts_before


@pytest.mark.asyncio
async def test_revise_label_sets_and_clears_on_both_input_kinds():
    fake = _fake_with_inputs(audio={'transcript': 'machine'}, note={'text': 'n'})

    async with _client(fake) as client:
        service = _service()
        for entry_id, kind in (('a1', 'audio'), ('n1', 'note')):
            assert (
                await service.revise_input_label(
                    client,
                    UPLOAD_ID,
                    entry_id,
                    '  spin coating ',
                    collection_entry_id=SAND_COLLECTION_ID,
                )
                == kind
            )
            data = fake.archive(f'{entry_id}.archive.json')['data']
            assert data['label'] == 'spin coating'

            await service.revise_input_label(
                client, UPLOAD_ID, entry_id, '', collection_entry_id=SAND_COLLECTION_ID
            )
            assert 'label' not in fake.archive(f'{entry_id}.archive.json')['data']

    # the label edit leaves the input's content alone
    assert fake.archive('a1.archive.json')['data']['transcript'] == 'machine'
    assert fake.archive('n1.archive.json')['data']['text'] == 'n'


@pytest.mark.asyncio
async def test_revise_label_rejects_an_entry_outside_the_collection():
    fake = _fake_with_inputs(audio={}, note={'text': 'n'})
    puts_before = fake.put_attempts

    async with _client(fake) as client:
        with pytest.raises(NomadAPIError) as excinfo:
            await _service().revise_input_label(
                client,
                UPLOAD_ID,
                'x1',
                'hijack',
                collection_entry_id=SAND_COLLECTION_ID,
            )

    assert excinfo.value.status_code == HTTPStatus.NOT_FOUND
    assert fake.put_attempts == puts_before


@pytest.mark.asyncio
async def test_revise_datetime_reorders_the_inputs():
    fake = _fake_with_inputs(
        audio={'datetime': '2026-09-22T10:00:00+00:00'},
        note={'text': 'n', 'datetime': '2026-09-22T09:00:00+00:00'},
    )

    async with _client(fake) as client:
        service = _service()
        await service.revise_input_datetime(
            client,
            UPLOAD_ID,
            'n1',
            '2026-09-22T11:00:00Z',
            collection_entry_id=SAND_COLLECTION_ID,
        )
        inputs = await service.collect_inputs(client, UPLOAD_ID, SAND_COLLECTION_ID)

    assert [i.entry_id for i in inputs] == ['a1', 'n1']


@pytest.mark.asyncio
async def test_concurrent_revisions_of_one_input_keep_both_changes():
    fake = _fake_with_inputs(audio={'transcript': 'machine'}, note={'text': 'n'})

    async def interleaving(request: httpx.Request) -> httpx.Response:
        # yield on every request so the two revisions actually overlap
        await asyncio.sleep(0)
        return fake(request)

    async with _client(interleaving) as client:
        service = _service()
        await asyncio.gather(
            service.revise_input(
                client, UPLOAD_ID, 'a1', 'fixed', collection_entry_id=SAND_COLLECTION_ID
            ),
            service.revise_input_datetime(
                client,
                UPLOAD_ID,
                'a1',
                '2026-09-22T11:00:00Z',
                collection_entry_id=SAND_COLLECTION_ID,
            ),
        )

    audio = fake.archive('a1.archive.json')['data']
    assert audio['corrected_transcript'] == 'fixed'
    assert audio['datetime'] == '2026-09-22T11:00:00+00:00'
