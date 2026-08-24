import httpx
import pytest
from nomad.utils import generate_entry_id

from sand.services.nomad_upload import NomadAuthError
from sand.services.voice_eln import VoiceElnService

BASE_URL = 'http://localhost:8000/nomad-oasis/api/v1'
UPLOAD_ID = 'up-123'
FILENAME = 'rec.m4a'
ENTRY_ID = generate_entry_id(UPLOAD_ID, f'{FILENAME}.archive.json')


def _service() -> VoiceElnService:
    return VoiceElnService(BASE_URL)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_URL)


class _FakeNomad:
    """Programmable NOMAD API: create upload, receive audio."""

    def __init__(self):
        self.received_audio = None
        self.received_file_name = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == 'POST' and request.url.path.endswith('/uploads'):
            return httpx.Response(200, json={'upload_id': UPLOAD_ID})
        if request.method == 'PUT' and f'/uploads/{UPLOAD_ID}/raw/' in request.url.path:
            self.received_audio = request.content
            self.received_file_name = request.url.params.get('file_name')
            return httpx.Response(200, json={})
        return httpx.Response(404, json={'detail': f'unexpected {request.url.path}'})


@pytest.mark.asyncio
async def test_uploads_audio_and_returns_entry_link():
    fake = _FakeNomad()

    async with _client(fake) as client:
        result = await _service().create_audio_entry(client, b'AUDIO', FILENAME)

    assert result.upload_id == UPLOAD_ID
    assert result.entry_id == ENTRY_ID
    assert UPLOAD_ID in result.entry_url and ENTRY_ID in result.entry_url
    # the audio bytes were uploaded under the original filename, so the
    # voice-eln parser matches the extension
    assert fake.received_audio == b'AUDIO'
    assert fake.received_file_name == FILENAME


@pytest.mark.asyncio
async def test_invalid_token_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={'detail': 'unauthorized'})

    async with _client(handler) as client:
        with pytest.raises(NomadAuthError):
            await _service().create_audio_entry(client, b'AUDIO', FILENAME)


def test_build_client_sends_bearer_token():
    client = _service().build_client('tok-1')
    assert client.headers['Authorization'] == 'Bearer tok-1'
