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

    async def list_experiments(
        self, client: httpx.AsyncClient
    ) -> list[ExperimentSummary]:
        """The user's unpublished experiments (published ones are read-only)."""
        response = await client.post(
            '/entries/query',
            json={
                'owner': 'user',
                'query': {
                    'section_defs.definition_qualified_name': (INPUT_COLLECTION_M_DEF),
                    'published': False,
                },
                'required': {
                    'include': ['entry_id', 'upload_id', 'entry_name', 'mainfile']
                },
                'pagination': {
                    'page_size': 100,
                    'order_by': 'entry_create_time',
                    'order': 'desc',
                },
            },
        )
        self._check_response(response, step='list_experiments')
        entries = response.json().get('data', [])
        return [
            ExperimentSummary(
                upload_id=entry['upload_id'],
                entry_id=entry['entry_id'],
                name=entry.get('entry_name') or entry['upload_id'],
            )
            for entry in entries
        ]

    async def create_hysprint_experiment(
        self, client: httpx.AsyncClient, name: str, info: dict | None
    ) -> EntryHandle:
        """Create a hysprint experiment upload with its InputCollection entry.

        Hysprint-specific: `info` is the hysprint experiment-info form
        (project_name, batch, subbatch, first_sample, n_samples). If given,
        it is stored as a WrittenNote labeled 'experiment_info' and
        referenced from the collection, so the Generate step can route it
        to the form parser instead of the step extractor. The collection
        and note plumbing itself is generic voice-eln.
        """
        upload_id = await self._create_upload(client, upload_name=name)
        now = _utc_now_iso()

        notes = []
        if info is not None:
            info_mainfile = 'experiment_info.archive.json'
            await self._write_archive(
                client,
                upload_id,
                info_mainfile,
                {
                    'data': {
                        'm_def': WRITTEN_NOTE_M_DEF,
                        'name': 'Experiment info',
                        'datetime': now,
                        'text': json.dumps(info),
                        'label': EXPERIMENT_INFO_LABEL,
                    }
                },
            )
            notes.append(
                entry_ref(upload_id, generate_entry_id(upload_id, info_mainfile))
            )

        collection = {
            'm_def': INPUT_COLLECTION_M_DEF,
            'name': name,
            'datetime': now,
        }
        if notes:
            collection['notes'] = notes
        await self._write_archive(
            client, upload_id, EXPERIMENT_MAINFILE, {'data': collection}
        )
        return EntryHandle(
            upload_id=upload_id,
            entry_id=generate_entry_id(upload_id, EXPERIMENT_MAINFILE),
        )

    async def add_audio(
        self, client: httpx.AsyncClient, upload_id: str, audio: bytes, filename: str
    ) -> EntryHandle:
        """Add a recording to an experiment.

        Uploads the audio file into the experiment upload (the voice-eln
        parser turns it into an AudioInput entry and transcribes it) and
        references the entry from the experiment's InputCollection.
        """
        # Timestamp prefix: recordings all arrive as e.g. 'recording.webm',
        # and a second file with the same name would overwrite the first.
        stored_name = f'{_utc_now_stamp()}_{posixpath.basename(filename)}'
        await self._upload_raw_file(
            client, upload_id, stored_name, audio, 'application/octet-stream'
        )

        # The parser creates the AudioInput entry under a deterministic
        # companion mainfile, so the entry id is known before the entry exists.
        entry_id = generate_entry_id(upload_id, f'{stored_name}.archive.json')
        await self._append_to_collection(client, upload_id, 'audios', entry_id)
        return EntryHandle(upload_id=upload_id, entry_id=entry_id)

    async def add_note(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        text: str,
        label: str = STEP_LABEL,
    ) -> EntryHandle:
        """Add a written note to an experiment and reference it in the collection."""
        now = _utc_now_iso()
        mainfile = f'note_{_utc_now_stamp()}.archive.json'
        await self._write_archive(
            client,
            upload_id,
            mainfile,
            {
                'data': {
                    'm_def': WRITTEN_NOTE_M_DEF,
                    'name': f'Note {now}',
                    'datetime': now,
                    'text': text,
                    'label': label,
                }
            },
        )
        entry_id = generate_entry_id(upload_id, mainfile)
        await self._append_to_collection(client, upload_id, 'notes', entry_id)
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
        """PUT a raw file, retrying while the upload is busy processing.

        Every raw-file write triggers processing, so a follow-up write to
        the same upload (info note then collection; audio then collection
        append) is rejected with 400 'blocked by another process' until
        processing finishes. Retry until it does or the deadline passes.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._write_timeout_s
        while True:
            response = await client.put(
                f'/uploads/{upload_id}/raw/',
                params={'file_name': file_name},
                content=content,
                headers={'Content-Type': content_type},
            )
            if (
                response.status_code == HTTPStatus.BAD_REQUEST
                and 'blocked by another process' in response.text
            ):
                if loop.time() >= deadline:
                    raise NomadAPIError(
                        response.status_code,
                        f'Upload {upload_id} still processing after '
                        f'{self._write_timeout_s:.0f}s; could not write '
                        f'{file_name}',
                        step='upload_raw_file',
                    )
                await asyncio.sleep(self._retry_interval_s)
                continue
            self._check_response(response, step='upload_raw_file')
            return

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
        self, client: httpx.AsyncClient, upload_id: str, field: str, entry_id: str
    ) -> None:
        """Reference a new entry from the experiment's InputCollection.

        Read-modify-write of the collection mainfile; concurrent uploads to
        the same experiment can race here (accepted for now, see the design
        discussion).
        """
        mainfile = await self._find_collection_mainfile(client, upload_id)
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

        data = archive.setdefault('data', {})
        refs = data.setdefault(field, [])
        ref = entry_ref(upload_id, entry_id)
        if ref not in refs:
            refs.append(ref)
            await self._write_archive(client, upload_id, mainfile, archive)

    async def _find_collection_mainfile(
        self, client: httpx.AsyncClient, upload_id: str
    ) -> str:
        """Mainfile of the InputCollection entry in this upload.

        Queried so experiments created directly in NOMAD (any mainfile name)
        also work; falls back to sand's own EXPERIMENT_MAINFILE when the
        entry is not indexed yet (right after create_hysprint_experiment).
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
                    'page_size': 1,
                    'order_by': 'entry_create_time',
                    'order': 'asc',
                },
            },
        )
        self._check_response(response, step='find_collection')
        entries = response.json().get('data', [])
        if entries:
            return entries[0]['mainfile']
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
