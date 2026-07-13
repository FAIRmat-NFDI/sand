import json
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import httpx

# Mainfile name the archive is uploaded under (see NomadUploader.upload).
ARCHIVE_FILENAME = 'entry.archive.json'

# Lowest HTTP status code that counts as an error response.
HTTP_ERROR_STATUS = 400


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

    async def upload(self, archive: dict, token: str) -> UploadResult:
        """Upload a single archive dict as entry.archive.json."""
        async with self.build_client(token) as client:
            return await self.upload_with_client(client, archive)

    async def upload_with_client(
        self, client: httpx.AsyncClient, archive: dict
    ) -> UploadResult:
        """Upload one archive over an existing client.

        Lets callers reuse a single connection pool across several uploads.
        """
        response = await client.post('/uploads')
        self._check_response(response, step='create_upload')

        try:
            upload_id = response.json()['upload_id']
        except (ValueError, TypeError, KeyError):
            raise NomadAPIError(
                0,
                f'Expected JSON with upload_id, got: {response.text[:500]}',
                step='create_upload',
            )

        response = await client.put(
            f'/uploads/{upload_id}/raw/',
            params={'file_name': ARCHIVE_FILENAME},
            content=json.dumps(archive).encode(),
            headers={'Content-Type': 'application/json'},
        )
        self._check_response(response, step='write_archive')

        return UploadResult(
            upload_id=upload_id, entry_url=self._entry_url(upload_id)
        )

    def _entry_url(self, upload_id: str) -> str:
        """Build the NOMAD GUI URL for an upload from the configured API base URL."""
        parsed = urlparse(self._base_url)
        path = parsed.path.rstrip('/')
        for suffix in ('/api/v1', '/api'):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break

        netloc = parsed.netloc
        # TODO: remove the localhost logic in production
        # hostname is None when base_url has no scheme; treat that as non-local.
        if 'localhost' in (parsed.hostname or ''):
            netloc = netloc.replace(':8000', ':3000')
        # NOMAD GUI v1 (classic) upload URL:
        #   {base}/gui/user/uploads/upload/id/{upload_id}
        gui_base = urlunparse((parsed.scheme, netloc, f'{path}/gui', '', '', ''))
        return f'{gui_base}/user/uploads/upload/id/{upload_id}'

    def _check_response(self, response: httpx.Response, step: str) -> None:
        if response.status_code in (401, 403):
            raise NomadAuthError(response.status_code, response.text, step=step)
        if response.status_code >= HTTP_ERROR_STATUS:
            raise NomadAPIError(response.status_code, response.text, step=step)

