import json
from dataclasses import dataclass

import httpx


class NomadAPIError(Exception):
    def __init__(self, status_code: int, detail: str, step: str) -> None:
        self.status_code = status_code
        self.detail = detail
        self.step = step
        super().__init__(f'NOMAD API error at {step} ({status_code}): {detail}')


class NomadAuthError(NomadAPIError):
    pass


@dataclass
class UploadResult:
    upload_id: str
    entry_url: str


class NomadUploader:
    def __init__(self, base_url: str, token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/json',
            },
            timeout=httpx.Timeout(30.0),
        )

    async def upload(self, archive: dict) -> UploadResult:
        """Upload a single archive dict as entry.archive.json."""
        response = await self._client.post('/uploads')
        self._check_response(response, step='create_upload')

        try:
            upload_id = response.json()['upload_id']
        except (ValueError, TypeError, KeyError):
            raise NomadAPIError(
                0,
                f'Expected JSON with upload_id, got: {response.text[:500]}',
                step='create_upload',
            )

        response = await self._client.put(
            f'/uploads/{upload_id}/raw/',
            params={'file_name': 'entry.archive.json'},
            content=json.dumps(archive).encode(),
            headers={'Content-Type': 'application/json'},
        )
        self._check_response(response, step='write_archive')

        api_base = str(self._client.base_url).rstrip('/')
        # TODO !!! hard code for now, need change
        entry_url = f"http://localhost:3000/nomad-oasis/gui/user/uploads/upload/id/{upload_id}"
        return UploadResult(upload_id=upload_id, entry_url=entry_url)

    def _check_response(self, response: httpx.Response, step: str) -> None:
        if response.status_code in (401, 403):
            raise NomadAuthError(response.status_code, response.text, step=step)
        if response.status_code >= 400:
            raise NomadAPIError(response.status_code, response.text, step=step)

    async def close(self) -> None:
        await self._client.aclose()
