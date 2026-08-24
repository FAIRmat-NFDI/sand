from dataclasses import dataclass

import httpx
from nomad.utils import generate_entry_id

from sand.services.nomad_upload import (
    HTTP_ERROR_STATUS,
    NomadAPIError,
    NomadAuthError,
    gui_upload_url,
)


@dataclass
class AudioEntryResult:
    upload_id: str
    entry_id: str
    entry_url: str


class VoiceElnService:
    """Create AudioInput entries owned by the nomad-voice-eln plugin.
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    def build_client(self, token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/json',
            },
            timeout=httpx.Timeout(30.0),
        )

    async def create_audio_entry(
        self, client: httpx.AsyncClient, audio: bytes, filename: str
    ) -> AudioEntryResult:
        """Upload the audio to a new NOMAD upload and return the entry URL."""
        upload_id = await self._create_upload(client)
        await self._upload_audio(client, upload_id, audio, filename)

        # The parser creates the AudioInput entry under a deterministic
        # companion mainfile, so the entry id is known before the entry exists.
        mainfile = f'{filename}.archive.json'
        entry_id = generate_entry_id(upload_id, mainfile)
        upload_url = gui_upload_url(self._base_url, upload_id)
        entry_url = f'{upload_url}/entry/id/{entry_id}'

        return AudioEntryResult(
            upload_id=upload_id, entry_id=entry_id, entry_url=entry_url
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

    def _check_response(self, response: httpx.Response, step: str) -> None:
        if response.status_code in (401, 403):
            raise NomadAuthError(response.status_code, response.text, step=step)
        if response.status_code >= HTTP_ERROR_STATUS:
            raise NomadAPIError(response.status_code, response.text, step=step)
