"""Generic NOMAD REST API plumbing: client, errors, refs, URLs, raw files."""

import asyncio
import json
import time
from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import urlparse, urlunparse

import httpx

# Lowest HTTP status code that counts as an error response.
HTTP_ERROR_STATUS = 400


def gui_upload_url(base_url: str, upload_id: str) -> str:
    """Build the NOMAD GUI URL for an upload from the API base URL."""
    parsed = urlparse(base_url)
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


def gui_entry_url(base_url: str, upload_id: str, entry_id: str) -> str:
    """Build the NOMAD GUI URL for one entry."""
    return f'{gui_upload_url(base_url, upload_id)}/entry/id/{entry_id}'


def entry_ref(upload_id: str, entry_id: str) -> str:
    """The reference string NOMAD's entry references use."""
    return f'../uploads/{upload_id}/archive/{entry_id}#/data'


def entry_id_from_ref(ref: str) -> str:
    """The entry id inside a '../uploads/{uid}/archive/{eid}#/data' reference."""
    _, sep, rest = str(ref).partition('/archive/')
    entry_id = rest.split('#', 1)[0].strip('/')
    if not sep or not entry_id or '/' in entry_id:
        raise NomadAPIError(
            0, f'Unparseable entry reference: {ref!r}', step='entry_ref'
        )
    return entry_id


class NomadAPIError(Exception):
    def __init__(self, status_code: int, detail: str, step: str) -> None:
        self.status_code = status_code
        self.detail = detail
        self.step = step
        super().__init__(f'NOMAD API error at {step} ({status_code}): {detail}')


class NomadAuthError(NomadAPIError):
    pass


def build_client(base_url: str, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/json',
        },
        timeout=httpx.Timeout(30.0),
    )


def check_response(response: httpx.Response, step: str) -> None:
    if response.status_code in (401, 403):
        raise NomadAuthError(response.status_code, response.text, step=step)
    if response.status_code >= HTTP_ERROR_STATUS:
        raise NomadAPIError(response.status_code, response.text, step=step)


async def create_upload(
    client: httpx.AsyncClient, upload_name: str | None = None
) -> str:
    params = {'upload_name': upload_name} if upload_name else None
    response = await client.post('/uploads', params=params)
    check_response(response, step='create_upload')
    try:
        return response.json()['upload_id']
    except (ValueError, TypeError, KeyError):
        raise NomadAPIError(
            0,
            f'Expected JSON with upload_id, got: {response.text[:500]}',
            step='create_upload',
        )


@dataclass(frozen=True)
class RawFileWriter:
    """Raw-file writes that wait out NOMAD's upload processing.

    Every raw-file write triggers processing, and NOMAD rejects further
    writes to the upload until it finishes. Poll the upload's
    process_running state and send the body only when the upload is idle —
    this avoids re-transmitting large files and does not depend on the
    wording of NOMAD's rejection message.
    """

    retry_interval_s: float = 1.0
    write_timeout_s: float = 60.0

    async def upload_raw_file(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        file_name: str,
        content: bytes,
        content_type: str,
    ) -> None:
        """PUT a raw file once the upload is idle. A 400 caused by
        processing that started between the check and the PUT is retried."""
        deadline = time.monotonic() + self.write_timeout_s
        # PUT raw only accepts a bare basename in file_name; the directory
        # goes into the URL path.
        directory, _, base_name = file_name.rpartition('/')
        while True:
            await self.wait_until_writable(client, upload_id, deadline, file_name)
            response = await client.put(
                f'/uploads/{upload_id}/raw/{directory}',
                params={'file_name': base_name},
                content=content,
                headers={'Content-Type': content_type},
            )
            if (
                response.status_code == HTTPStatus.BAD_REQUEST
                and time.monotonic() < deadline
                and await self._upload_is_processing(client, upload_id)
            ):
                continue
            check_response(response, step='upload_raw_file')
            return

    async def wait_until_writable(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        deadline: float,
        file_name: str,
    ) -> None:
        """Wait until the upload exists, is unpublished, and is not
        processing; raise a NomadAPIError with the real cause otherwise."""
        while True:
            response = await client.get(f'/uploads/{upload_id}')
            if response.status_code == HTTPStatus.NOT_FOUND:
                raise NomadAPIError(
                    HTTPStatus.NOT_FOUND,
                    f'Upload {upload_id} does not exist',
                    step='upload_status',
                )
            check_response(response, step='upload_status')
            try:
                data = response.json().get('data') or {}
            except ValueError:
                data = {}
            if data.get('published'):
                raise NomadAPIError(
                    HTTPStatus.BAD_REQUEST,
                    f'Upload {upload_id} is published and read-only',
                    step='upload_status',
                )
            if not data.get('process_running'):
                return
            if time.monotonic() >= deadline:
                raise NomadAPIError(
                    HTTPStatus.BAD_REQUEST,
                    f'Upload {upload_id} still processing after '
                    f'{self.write_timeout_s:.0f}s; could not write {file_name}',
                    step='upload_raw_file',
                )
            await asyncio.sleep(self.retry_interval_s)

    async def read_raw_file(
        self, client: httpx.AsyncClient, upload_id: str, file_name: str
    ) -> bytes | None:
        """The raw file's bytes once the upload is idle; None if absent."""
        await self.wait_until_writable(
            client, upload_id, time.monotonic() + self.write_timeout_s, file_name
        )
        response = await client.get(f'/uploads/{upload_id}/raw/{file_name}')
        if response.status_code == HTTPStatus.NOT_FOUND:
            return None
        check_response(response, step='read_raw_file')
        return response.content

    async def delete_raw_file(
        self, client: httpx.AsyncClient, upload_id: str, file_name: str
    ) -> None:
        """DELETE a raw file once the upload is idle (already-gone is fine).
        Deleting triggers processing, like a PUT, with the same race."""
        deadline = time.monotonic() + self.write_timeout_s
        while True:
            await self.wait_until_writable(client, upload_id, deadline, file_name)
            response = await client.delete(f'/uploads/{upload_id}/raw/{file_name}')
            if response.status_code == HTTPStatus.NOT_FOUND:
                return
            if (
                response.status_code == HTTPStatus.BAD_REQUEST
                and time.monotonic() < deadline
                and await self._upload_is_processing(client, upload_id)
            ):
                continue
            check_response(response, step='delete_raw_file')
            return

    async def write_archive(
        self, client: httpx.AsyncClient, upload_id: str, mainfile: str, archive: dict
    ) -> None:
        """Serialize the archive dict and PUT it as the raw mainfile."""
        await self.upload_raw_file(
            client,
            upload_id,
            mainfile,
            json.dumps(archive).encode(),
            'application/json',
        )

    async def read_archive(
        self, client: httpx.AsyncClient, upload_id: str, mainfile: str
    ) -> dict:
        """The raw mainfile's JSON, once the upload is idle.

        Raw files land in staging asynchronously after a PUT, so wait for
        processing to finish before reading - a plain GET right after a
        write can 404 even though the write succeeded.
        """
        await self.wait_until_writable(
            client, upload_id, time.monotonic() + self.write_timeout_s, mainfile
        )
        response = await client.get(f'/uploads/{upload_id}/raw/{mainfile}')
        if response.status_code == HTTPStatus.NOT_FOUND:
            raise NomadAPIError(
                HTTPStatus.NOT_FOUND,
                f'No mainfile {mainfile} found in upload {upload_id}',
                step='read_archive',
            )
        check_response(response, step='read_archive')
        try:
            archive = response.json()
        except ValueError:
            raise NomadAPIError(
                0, f'Mainfile {mainfile} is not valid JSON', step='read_archive'
            )
        if not isinstance(archive, dict):
            raise NomadAPIError(
                0, f'Mainfile {mainfile} is not a JSON object', step='read_archive'
            )
        return archive

    async def _upload_is_processing(
        self, client: httpx.AsyncClient, upload_id: str
    ) -> bool:
        response = await client.get(f'/uploads/{upload_id}')
        if response.status_code != HTTPStatus.OK:
            return False
        try:
            data = response.json().get('data') or {}
        except ValueError:
            return False
        return bool(data.get('process_running'))
