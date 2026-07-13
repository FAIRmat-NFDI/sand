import json
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import httpx

# Mainfile name the archive is uploaded under (see NomadUploader.upload).
ARCHIVE_FILENAME = 'entry.archive.json'


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

            parsed = urlparse(self._base_url)
            path = parsed.path.rstrip('/')
            for suffix in ('/api/v1', '/api'):
                if path.endswith(suffix):
                    path = path[: -len(suffix)]
                    break

            netloc = parsed.netloc
            # TODO: remove the localhost logic in production
            if 'localhost' in parsed.hostname:
                netloc = netloc.replace(':8000', ':3000')
            # NOMAD GUI v1 (classic) upload URL:
            #   {base}/gui/user/uploads/upload/id/{upload_id}
            gui_base = urlunparse((parsed.scheme, netloc, f'{path}/gui', '', '', ''))
            entry_url = f'{gui_base}/user/uploads/upload/id/{upload_id}'
            return UploadResult(upload_id=upload_id, entry_url=entry_url)

    def _check_response(self, response: httpx.Response, step: str) -> None:
        if response.status_code in (401, 403):
            raise NomadAuthError(response.status_code, response.text, step=step)
        if response.status_code >= 400:
            raise NomadAPIError(response.status_code, response.text, step=step)

