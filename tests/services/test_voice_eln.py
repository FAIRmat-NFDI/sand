import json

import httpx
import pytest
from nomad.utils import generate_entry_id

from sand.services.nomad_upload import NomadAuthError
from sand.services.voice_eln import (
    EXPERIMENT_MAINFILE,
    VoiceElnService,
    entry_ref,
)

BASE_URL = 'http://localhost:8000/nomad-oasis/api/v1'
UPLOAD_ID = 'up-123'


def _service() -> VoiceElnService:
    # retry_interval_s=0: no sleeps in tests
    return VoiceElnService(BASE_URL, retry_interval_s=0, write_timeout_s=5)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_URL)


class _FakeNomad:
    """Programmable NOMAD API: uploads, raw files, and entry queries.

    `blocked_writes` rejects that many PUTs with NOMAD's processing-lock
    error before accepting writes again.
    """

    def __init__(self, query_results=None, blocked_writes=0):
        self.raw_files: dict[str, bytes] = {}
        self.query_results = query_results or []
        self.blocked_writes = blocked_writes

    def _put_raw(self, request: httpx.Request) -> httpx.Response:
        if self.blocked_writes > 0:
            self.blocked_writes -= 1
            return httpx.Response(
                400,
                json={'detail': 'The upload is currently blocked by another process.'},
            )
        self.raw_files[request.url.params['file_name']] = request.content
        return httpx.Response(200, json={})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == 'POST' and path.endswith('/uploads'):
            return httpx.Response(200, json={'upload_id': UPLOAD_ID})
        if request.method == 'PUT' and f'/uploads/{UPLOAD_ID}/raw/' in path:
            return self._put_raw(request)
        if request.method == 'GET' and f'/uploads/{UPLOAD_ID}/raw/' in path:
            name = path.split('/raw/', 1)[1]
            if name not in self.raw_files:
                return httpx.Response(404, json={'detail': 'not found'})
            return httpx.Response(200, content=self.raw_files[name])
        if request.method == 'POST' and path.endswith('/entries/query'):
            return httpx.Response(200, json={'data': self.query_results})
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
        result = await _service().create_written_note(
            client,
            'perov_B1_a',
            text=json.dumps(INFO),
            label='experiment_info',
            note_mainfile='experiment_info.archive.json',
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
        await _service().create_written_note(client, 'scratch')

    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert 'notes' not in collection
    assert list(fake.raw_files) == [EXPERIMENT_MAINFILE]


@pytest.mark.asyncio
async def test_add_audio_stores_file_and_references_it_from_collection():
    fake = _FakeNomad()

    async with _client(fake) as client:
        service = _service()
        await service.create_written_note(client, 'perov_B1_a')
        result = await service.add_audio(client, UPLOAD_ID, b'AUDIO', 'rec.m4a')

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
async def test_add_note_writes_step_note_and_references_it():
    fake = _FakeNomad()

    async with _client(fake) as client:
        service = _service()
        await service.create_written_note(client, 'perov_B1_a')
        result = await service.add_note(client, UPLOAD_ID, 'spun coat at 2000 rpm')

    note_files = [n for n in fake.raw_files if n.startswith('note_')]
    assert len(note_files) == 1
    note = fake.archive(note_files[0])['data']
    assert note['text'] == 'spun coat at 2000 rpm'
    assert note['label'] == 'step'
    assert result.entry_id == generate_entry_id(UPLOAD_ID, note_files[0])
    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert collection['notes'] == [entry_ref(UPLOAD_ID, result.entry_id)]


@pytest.mark.asyncio
async def test_append_uses_collection_mainfile_from_query():
    # an experiment created directly in NOMAD can use any mainfile name
    fake = _FakeNomad(query_results=[{'mainfile': 'my_collection.archive.json'}])
    fake.raw_files['my_collection.archive.json'] = json.dumps(
        {'data': {'m_def': 'x', 'name': 'manual'}}
    ).encode()

    async with _client(fake) as client:
        result = await _service().add_note(client, UPLOAD_ID, 'a step')

    collection = fake.archive('my_collection.archive.json')['data']
    assert collection['notes'] == [entry_ref(UPLOAD_ID, result.entry_id)]


@pytest.mark.asyncio
async def test_list_input_collections_returns_summaries():
    fake = _FakeNomad(
        query_results=[
            {
                'upload_id': UPLOAD_ID,
                'entry_id': 'e-1',
                'entry_name': 'perov_B1_a',
                'mainfile': EXPERIMENT_MAINFILE,
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
async def test_write_retries_while_upload_is_processing():
    # the first PUT triggers processing; NOMAD rejects follow-up writes with
    # 400 'blocked by another process' until it finishes
    fake = _FakeNomad(blocked_writes=2)

    async with _client(fake) as client:
        result = await _service().create_written_note(
            client,
            'perov_B1_a',
            text=json.dumps(INFO),
            label='experiment_info',
            note_mainfile='experiment_info.archive.json',
        )

    assert result.upload_id == UPLOAD_ID
    assert 'experiment_info.archive.json' in fake.raw_files
    assert EXPERIMENT_MAINFILE in fake.raw_files


@pytest.mark.asyncio
async def test_invalid_token_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={'detail': 'unauthorized'})

    async with _client(handler) as client:
        with pytest.raises(NomadAuthError):
            await _service().create_written_note(client, 'x')


def test_build_client_sends_bearer_token():
    client = _service().build_client('tok-1')
    assert client.headers['Authorization'] == 'Bearer tok-1'
