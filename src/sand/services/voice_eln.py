import asyncio
import json
import posixpath
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus

import httpx
from nomad.utils import generate_entry_id

from sand.services.nomad_upload import (
    HTTP_ERROR_STATUS,
    NomadAPIError,
    NomadAuthError,
    gui_upload_url,
)

# Qualified names of the nomad-voice-eln schema sections sand instantiates.
INPUT_COLLECTION_M_DEF = (
    'nomad_voice_eln.schema_packages.schema_package.InputCollection'
)
WRITTEN_NOTE_M_DEF = 'nomad_voice_eln.schema_packages.schema_package.WrittenNote'

# Mainfile of the InputCollection entry in an experiment upload created by sand.
EXPERIMENT_MAINFILE = 'experiment.archive.json'

# Label routing (see docs/handover.md §8): the experiment-info form JSON is a
# WrittenNote labeled 'experiment_info'; step narrations are labeled 'step'.
EXPERIMENT_INFO_LABEL = 'experiment_info'
STEP_LABEL = 'step'

# Where the experiment-info form note is stored in the experiment upload.
EXPERIMENT_INFO_MAINFILE = 'experiment_info.archive.json'

# Audio types the nomad-voice-eln parser matches (its mainfile_name_re);
# a file with any other extension is stored but never becomes an entry.
AUDIO_EXTENSIONS = frozenset({'wav', 'mp3', 'm4a', 'flac', 'ogg', 'webm', 'opus'})

# Equivalent containers stored under an extension the parser matches:
# Safari's MediaRecorder produces audio/mp4, which is m4a (AAC in MPEG-4).
AUDIO_EXTENSION_ALIASES = {'mp4': 'm4a', 'mpeg': 'mp3', 'mpga': 'mp3', 'x-m4a': 'm4a'}


def normalize_audio_filename(filename: str) -> str | None:
    """The name to store an audio file under, or None if the voice-eln
    parser would not turn a file of this type into an AudioInput entry."""
    stem, dot, ext = filename.rpartition('.')
    if not dot:
        return None
    ext = AUDIO_EXTENSION_ALIASES.get(ext.lower(), ext.lower())
    if ext not in AUDIO_EXTENSIONS:
        return None
    return f'{stem}.{ext}'


def entry_ref(upload_id: str, entry_id: str) -> str:
    """The reference string NOMAD's ReferenceEditQuantity uses for entries."""
    return f'../uploads/{upload_id}/archive/{entry_id}#/data'


@dataclass
class EntryHandle:
    upload_id: str
    entry_id: str


@dataclass
class ExperimentSummary:
    upload_id: str
    entry_id: str
    name: str


class VoiceElnService:
    """Manage voice-eln entries for sand experiments.

    One experiment = one NOMAD upload holding an InputCollection entry
    (EXPERIMENT_MAINFILE) plus the WrittenNote entries and audio files.
     Audio files are turned into AudioInput entries by the
    nomad-voice-eln parser and transcribed inside NOMAD.
    """

    def __init__(
        self,
        base_url: str,
        retry_interval_s: float = 1.0,
        write_timeout_s: float = 60.0,
    ) -> None:
        self._base_url = base_url
        self._retry_interval_s = retry_interval_s
        self._write_timeout_s = write_timeout_s

    def build_client(self, token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/json',
            },
            timeout=httpx.Timeout(30.0),
        )

    def entry_url(self, upload_id: str, entry_id: str) -> str:
        upload_url = gui_upload_url(self._base_url, upload_id)
        return f'{upload_url}/entry/id/{entry_id}'

    async def list_input_collections(
        self, client: httpx.AsyncClient
    ) -> list[ExperimentSummary]:
        """The user's unpublished InputCollection entries (published ones
        are read-only)."""
        entries: list[dict] = []
        page_after_value = None
        while True:
            pagination = {
                'page_size': 100,
                'order_by': 'entry_create_time',
                'order': 'desc',
            }
            if page_after_value:
                pagination['page_after_value'] = page_after_value
            response = await client.post(
                '/entries/query',
                json={
                    'owner': 'user',
                    'query': {
                        'section_defs.definition_qualified_name': (
                            INPUT_COLLECTION_M_DEF
                        ),
                        'published': False,
                    },
                    'required': {'include': ['entry_id', 'upload_id', 'entry_name']},
                    'pagination': pagination,
                },
            )
            self._check_response(response, step='list_input_collections')
            body = response.json()
            page = body.get('data', [])
            entries.extend(page)
            page_after_value = body.get('pagination', {}).get('next_page_after_value')
            if not page_after_value or not page:
                break
        return [
            ExperimentSummary(
                upload_id=entry['upload_id'],
                entry_id=entry['entry_id'],
                name=entry.get('entry_name') or entry['upload_id'],
            )
            for entry in entries
        ]

    async def create_input_collection(
        self,
        client: httpx.AsyncClient,
        name: str,
        collection_mainfile: str,
        upload_id: str | None = None,
    ) -> EntryHandle:
        if upload_id is None:
            upload_id = await self._create_upload(client, upload_name=name)
        await self._write_archive(
            client,
            upload_id,
            collection_mainfile,
            {
                'data': {
                    'm_def': INPUT_COLLECTION_M_DEF,
                    'name': name,
                    'datetime': _utc_now_iso(),
                }
            },
        )
        return EntryHandle(
            upload_id=upload_id,
            entry_id=generate_entry_id(upload_id, collection_mainfile),
        )

    async def create_written_note(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        text: str,
        label: str = STEP_LABEL,
        note_mainfile: str | None = None,
    ) -> EntryHandle:
        mainfile = note_mainfile or f'note_{_utc_now_stamp()}.archive.json'
        await self._write_archive(
            client,
            upload_id,
            mainfile,
            {
                'data': {
                    'm_def': WRITTEN_NOTE_M_DEF,
                    'name': label,
                    'datetime': _utc_now_iso(),
                    'text': text,
                    'label': label,
                }
            },
        )
        return EntryHandle(
            upload_id=upload_id,
            entry_id=generate_entry_id(upload_id, mainfile),
        )

    async def add_written_note(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        text: str,
        label: str = STEP_LABEL,
        note_mainfile: str | None = None,
    ) -> EntryHandle:
        """Create a WrittenNote entry and reference it from the collection."""
        note = await self.create_written_note(
            client, upload_id, text, label, note_mainfile
        )
        await self._append_to_collection(client, upload_id, 'notes', note.entry_id)
        return note

    async def add_audio(
        self, client: httpx.AsyncClient, upload_id: str, audio: bytes, filename: str
    ) -> EntryHandle:
        """Add a recording to an experiment.

        Uploads the audio file into the experiment upload (the voice-eln
        parser turns it into an AudioInput entry and transcribes it) and
        references the entry from the experiment's InputCollection.
        """
        # Fail before storing the audio if there is no collection to
        # reference it from; otherwise the file would sit orphaned in the
        # upload and every retry would deposit another copy.
        mainfile = await self._find_collection_mainfile(client, upload_id)
        await self._read_collection(client, upload_id, mainfile)

        # Timestamp prefix: recordings all arrive as e.g. 'recording.webm',
        # and a second file with the same name would overwrite the first.
        stored_name = f'{_utc_now_stamp()}_{posixpath.basename(filename)}'
        await self._upload_raw_file(
            client, upload_id, stored_name, audio, 'application/octet-stream'
        )

        # The parser creates the AudioInput entry under a deterministic
        # companion mainfile, so the entry id is known before the entry exists.
        entry_id = generate_entry_id(upload_id, f'{stored_name}.archive.json')
        await self._append_to_collection(
            client, upload_id, 'audios', entry_id, mainfile=mainfile
        )
        return EntryHandle(upload_id=upload_id, entry_id=entry_id)

    async def _create_upload(
        self, client: httpx.AsyncClient, upload_name: str | None = None
    ) -> str:
        params = {'upload_name': upload_name} if upload_name else None
        response = await client.post('/uploads', params=params)
        self._check_response(response, step='create_upload')
        try:
            return response.json()['upload_id']
        except (ValueError, TypeError, KeyError):
            raise NomadAPIError(
                0,
                f'Expected JSON with upload_id, got: {response.text[:500]}',
                step='create_upload',
            )

    async def _upload_raw_file(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        file_name: str,
        content: bytes,
        content_type: str,
    ) -> None:
        """PUT a raw file once the upload is idle.

        Every raw-file write triggers processing, so a follow-up write to
        the same upload (info note then collection; audio then collection
        append) is rejected until processing finishes. Poll the upload's
        process_running state and send the body only when the upload is
        idle — this avoids re-transmitting large files and does not depend
        on the wording of NOMAD's rejection message. A 400 caused by
        processing that started between the check and the PUT is retried.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._write_timeout_s
        # PUT raw only accepts a bare basename in file_name; the directory
        # goes into the URL path (collections found in NOMAD-created
        # uploads can live in subdirectories).
        directory, _, base_name = file_name.rpartition('/')
        while True:
            await self._wait_until_writable(client, upload_id, deadline, file_name)
            response = await client.put(
                f'/uploads/{upload_id}/raw/{directory}',
                params={'file_name': base_name},
                content=content,
                headers={'Content-Type': content_type},
            )
            if (
                response.status_code == HTTPStatus.BAD_REQUEST
                and loop.time() < deadline
                and await self._upload_is_processing(client, upload_id)
            ):
                continue
            self._check_response(response, step='upload_raw_file')
            return

    async def _wait_until_writable(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        deadline: float,
        file_name: str,
    ) -> None:
        """Wait until the upload exists, is unpublished, and is not
        processing; raise a NomadAPIError with the real cause otherwise."""
        loop = asyncio.get_running_loop()
        while True:
            response = await client.get(f'/uploads/{upload_id}')
            if response.status_code == HTTPStatus.NOT_FOUND:
                raise NomadAPIError(
                    HTTPStatus.NOT_FOUND,
                    f'Upload {upload_id} does not exist',
                    step='upload_status',
                )
            self._check_response(response, step='upload_status')
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
            if loop.time() >= deadline:
                raise NomadAPIError(
                    HTTPStatus.BAD_REQUEST,
                    f'Upload {upload_id} still processing after '
                    f'{self._write_timeout_s:.0f}s; could not write {file_name}',
                    step='upload_raw_file',
                )
            await asyncio.sleep(self._retry_interval_s)

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

    async def _write_archive(
        self, client: httpx.AsyncClient, upload_id: str, mainfile: str, archive: dict
    ) -> None:
        await self._upload_raw_file(
            client,
            upload_id,
            mainfile,
            json.dumps(archive).encode(),
            'application/json',
        )

    async def _append_to_collection(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        field: str,
        entry_id: str,
        mainfile: str | None = None,
    ) -> None:
        """Reference a new entry from the experiment's InputCollection.

        Read-modify-write of the collection mainfile; concurrent uploads to
        the same experiment can race here (accepted for now, see the design
        discussion).
        """
        if mainfile is None:
            mainfile = await self._find_collection_mainfile(client, upload_id)
        archive = await self._read_collection(client, upload_id, mainfile)
        data = archive.get('data')
        if not isinstance(data, dict):
            raise NomadAPIError(
                0,
                f'Experiment mainfile {mainfile} has no data section',
                step='read_collection',
            )
        refs = data.get(field)
        if not isinstance(refs, list):
            # absent, or explicitly null (NOMAD's "unset" value): start fresh
            refs = []
            data[field] = refs
        ref = entry_ref(upload_id, entry_id)
        if ref not in refs:
            refs.append(ref)
            await self._write_archive(client, upload_id, mainfile, archive)

    async def _read_collection(
        self, client: httpx.AsyncClient, upload_id: str, mainfile: str
    ) -> dict:
        """The collection mainfile's JSON, once the upload is idle.

        Raw files land in staging asynchronously after a PUT, so wait for
        processing to finish before reading — a plain GET right after a
        write can 404 even though the write succeeded.
        """
        loop = asyncio.get_running_loop()
        await self._wait_until_writable(
            client, upload_id, loop.time() + self._write_timeout_s, mainfile
        )
        response = await client.get(f'/uploads/{upload_id}/raw/{mainfile}')
        if response.status_code == HTTPStatus.NOT_FOUND:
            raise NomadAPIError(
                HTTPStatus.NOT_FOUND,
                f'No experiment entry ({mainfile}) found in upload {upload_id}',
                step='read_collection',
            )
        self._check_response(response, step='read_collection')
        try:
            archive = response.json()
        except ValueError:
            raise NomadAPIError(
                0,
                f'Experiment mainfile {mainfile} is not valid JSON',
                step='read_collection',
            )
        if not isinstance(archive, dict):
            raise NomadAPIError(
                0,
                f'Experiment mainfile {mainfile} is not a JSON object',
                step='read_collection',
            )
        return archive

    async def _find_collection_mainfile(
        self, client: httpx.AsyncClient, upload_id: str
    ) -> str:
        """Mainfile of the InputCollection entry in this upload.

        Queried so experiments created directly in NOMAD (any mainfile name)
        also work; falls back to sand's own EXPERIMENT_MAINFILE when the
        entry is not indexed yet (right after create_input_collection).
        """
        response = await client.post(
            '/entries/query',
            json={
                'owner': 'visible',
                'query': {
                    'upload_id': upload_id,
                    'section_defs.definition_qualified_name': (INPUT_COLLECTION_M_DEF),
                },
                'required': {'include': ['mainfile']},
                'pagination': {
                    'page_size': 2,
                    'order_by': 'entry_create_time',
                    'order': 'asc',
                },
            },
        )
        self._check_response(response, step='find_collection')
        entries = response.json().get('data', [])
        mainfiles = [entry['mainfile'] for entry in entries]
        if len(mainfiles) > 1:
            # Ambiguous target: prefer sand's own collection if present,
            # otherwise refuse rather than silently pick the oldest.
            if EXPERIMENT_MAINFILE in mainfiles:
                return EXPERIMENT_MAINFILE
            raise NomadAPIError(
                HTTPStatus.BAD_REQUEST,
                f'Upload {upload_id} contains more than one InputCollection '
                'entry; cannot decide which experiment to add to',
                step='find_collection',
            )
        if mainfiles:
            return mainfiles[0]
        return EXPERIMENT_MAINFILE

    def _check_response(self, response: httpx.Response, step: str) -> None:
        if response.status_code in (401, 403):
            raise NomadAuthError(response.status_code, response.text, step=step)
        if response.status_code >= HTTP_ERROR_STATUS:
            raise NomadAPIError(response.status_code, response.text, step=step)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now_stamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
