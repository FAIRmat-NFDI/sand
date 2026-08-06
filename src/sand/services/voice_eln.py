import asyncio
from dataclasses import dataclass
from http import HTTPStatus

import httpx
from nomad.utils import generate_entry_id

from sand.services.nomad_upload import (
    HTTP_ERROR_STATUS,
    NomadAPIError,
    NomadAuthError,
    gui_upload_url,
)


class TranscriptionFailedError(RuntimeError):
    """The voice-eln transcription action reported FAILED for the entry."""

    def __init__(self, entry_url: str) -> None:
        self.entry_url = entry_url
        super().__init__(
            f'Transcription failed on the NOMAD side; see the entry: {entry_url}'
        )


class TranscriptionTimeoutError(TimeoutError):
    """No transcript within the polling window; NOMAD keeps transcribing."""

    def __init__(self, entry_url: str, timeout_s: float) -> None:
        self.entry_url = entry_url
        super().__init__(
            f'No transcript after {timeout_s:.0f}s; transcription continues in '
            f'NOMAD - check the entry: {entry_url}'
        )


@dataclass
class AudioEntryResult:
    text: str
    upload_id: str
    entry_id: str
    entry_url: str


class VoiceElnService:
    """Create AudioInput entries (voice-eln plugin) and read their transcripts.

    sand uploads only the audio file; the voice-eln parser turns it into an
    AudioInput entry and its transcription action runs Whisper inside NOMAD.
    This service then polls the entry archive for the machine transcript.
    """

    def __init__(
        self,
        base_url: str,
        poll_interval_s: float = 2.0,
        timeout_s: float = 120.0,
    ) -> None:
        self._base_url = base_url
        self._poll_interval_s = poll_interval_s
        self._timeout_s = timeout_s

    def build_client(self, token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/json',
            },
            timeout=httpx.Timeout(30.0),
        )

    async def transcribe_via_entry(
        self, client: httpx.AsyncClient, audio: bytes, filename: str
    ) -> AudioEntryResult:
        """Upload the audio to a new NOMAD upload and wait for its transcript."""
        upload_id = await self._create_upload(client)
        await self._upload_audio(client, upload_id, audio, filename)

        # The parser creates the AudioInput entry under a deterministic
        # companion mainfile, so the entry id is known before the entry exists.
        mainfile = f'{filename}.archive.json'
        entry_id = generate_entry_id(upload_id, mainfile)
        upload_url = gui_upload_url(self._base_url, upload_id)
        entry_url = f'{upload_url}/entry/id/{entry_id}'

        text = await self._wait_for_transcript(client, entry_id, entry_url)
        return AudioEntryResult(
            text=text, upload_id=upload_id, entry_id=entry_id, entry_url=entry_url
        )

    async def _create_upload(self, client: httpx.AsyncClient) -> str:
        response = await client.post('/uploads')
        self._check_response(response, step='create_upload')
        try:
            return response.json()['upload_id']
        except (ValueError, TypeError, KeyError):
            raise NomadAPIError(
                0,
                f'Expected JSON with upload_id, got: {response.text[:500]}',
                step='create_upload',
            )

    async def _upload_audio(
        self, client: httpx.AsyncClient, upload_id: str, audio: bytes, filename: str
    ) -> None:
        response = await client.put(
            f'/uploads/{upload_id}/raw/',
            params={'file_name': filename},
            content=audio,
            headers={'Content-Type': 'application/octet-stream'},
        )
        self._check_response(response, step='upload_audio')

    async def _wait_for_transcript(
        self, client: httpx.AsyncClient, entry_id: str, entry_url: str
    ) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_s
        while True:
            response = await client.get(f'/entries/{entry_id}/archive')
            if response.status_code == HTTPStatus.NOT_FOUND:
                # parsing/processing has not created the entry yet
                pass
            else:
                self._check_response(response, step='read_archive')
                data = (
                    response.json().get('data', {}).get('archive', {}).get('data', {})
                )
                text = data.get('whisper_transcript')
                if text is not None:
                    return text
                if data.get('transcription_status') == 'FAILED':
                    raise TranscriptionFailedError(entry_url)

            if loop.time() >= deadline:
                raise TranscriptionTimeoutError(entry_url, self._timeout_s)
            await asyncio.sleep(self._poll_interval_s)

    def _check_response(self, response: httpx.Response, step: str) -> None:
        if response.status_code in (401, 403):
            raise NomadAuthError(response.status_code, response.text, step=step)
        if response.status_code >= HTTP_ERROR_STATUS:
            raise NomadAPIError(response.status_code, response.text, step=step)
