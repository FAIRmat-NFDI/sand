import httpx
import pytest
from nomad.utils import generate_entry_id

from sand.services.nomad_upload import NomadAuthError
from sand.services.voice_eln import (
    TranscriptionFailedError,
    TranscriptionTimeoutError,
    VoiceElnService,
)

BASE_URL = 'http://localhost:8000/nomad-oasis/api/v1'
UPLOAD_ID = 'up-123'
FILENAME = 'rec.m4a'
ENTRY_ID = generate_entry_id(UPLOAD_ID, f'{FILENAME}.archive.json')


def _service() -> VoiceElnService:
    # poll_interval_s=0: no sleeps in tests
    return VoiceElnService(BASE_URL, poll_interval_s=0, timeout_s=5)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_URL)


def _archive_response(data: dict) -> httpx.Response:
    return httpx.Response(200, json={'data': {'archive': {'data': data}}})


class _FakeNomad:
    """Programmable NOMAD API: create upload, receive audio, serve archives.

    `archive_responses` is consumed one per poll; the last one repeats.
    """

    def __init__(self, archive_responses):
        self.archive_responses = list(archive_responses)
        self.received_audio = None
        self.received_file_name = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == 'POST' and request.url.path.endswith('/uploads'):
            return httpx.Response(200, json={'upload_id': UPLOAD_ID})
        if request.method == 'PUT' and f'/uploads/{UPLOAD_ID}/raw/' in request.url.path:
            self.received_audio = request.content
            self.received_file_name = request.url.params.get('file_name')
            return httpx.Response(200, json={})
        if (
            request.method == 'GET'
            and f'/entries/{ENTRY_ID}/archive' in request.url.path
        ):
            if len(self.archive_responses) > 1:
                return self.archive_responses.pop(0)
            return self.archive_responses[0]
        return httpx.Response(404, json={'detail': f'unexpected {request.url.path}'})


@pytest.mark.asyncio
async def test_uploads_audio_and_returns_transcript_when_it_arrives():
    fake = _FakeNomad(
        [
            # entry not processed yet, then processed without transcript (run in
            # flight), then the transcript arrives
            httpx.Response(404, json={'detail': 'no archive'}),
            _archive_response({'transcription_status': 'PENDING'}),
            _archive_response(
                {
                    'whisper_transcript': 'spun coat at 2000 rpm',
                    'transcription_status': 'COMPLETED',
                }
            ),
        ]
    )

    async with _client(fake) as client:
        result = await _service().transcribe_via_entry(client, b'AUDIO', FILENAME)

    assert result.text == 'spun coat at 2000 rpm'
    assert result.upload_id == UPLOAD_ID
    assert result.entry_id == ENTRY_ID
    assert UPLOAD_ID in result.entry_url and ENTRY_ID in result.entry_url
    # the audio bytes were uploaded under the original filename, so the
    # voice-eln parser matches the extension
    assert fake.received_audio == b'AUDIO'
    assert fake.received_file_name == FILENAME


@pytest.mark.asyncio
async def test_failed_transcription_raises_with_entry_url():
    fake = _FakeNomad([_archive_response({'transcription_status': 'FAILED'})])

    async with _client(fake) as client:
        with pytest.raises(TranscriptionFailedError) as excinfo:
            await _service().transcribe_via_entry(client, b'AUDIO', FILENAME)

    assert ENTRY_ID in excinfo.value.entry_url


@pytest.mark.asyncio
async def test_timeout_raises_with_entry_url():
    # the entry never gets processed within the timeout; the error carries the
    # entry URL so the user can check NOMAD later (transcription continues there)
    fake = _FakeNomad([httpx.Response(404, json={'detail': 'no archive'})])
    service = VoiceElnService(BASE_URL, poll_interval_s=0, timeout_s=0.05)

    async with _client(fake) as client:
        with pytest.raises(TranscriptionTimeoutError) as excinfo:
            await service.transcribe_via_entry(client, b'AUDIO', FILENAME)

    assert ENTRY_ID in excinfo.value.entry_url


@pytest.mark.asyncio
async def test_invalid_token_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={'detail': 'unauthorized'})

    async with _client(handler) as client:
        with pytest.raises(NomadAuthError):
            await _service().transcribe_via_entry(client, b'AUDIO', FILENAME)


def test_build_client_sends_bearer_token():
    client = _service().build_client('tok-1')
    assert client.headers['Authorization'] == 'Bearer tok-1'
