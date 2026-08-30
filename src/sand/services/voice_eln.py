import asyncio
import posixpath
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus

import httpx
from nomad.utils import generate_entry_id

from sand.services.nomad_api import (
    NomadAPIError,
    RawFileWriter,
    build_client,
    check_response,
    create_upload,
    entry_id_from_ref,
    entry_ref,
    gui_entry_url,
)

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
# a file with any other extension is stored but never becomes an audio entry.
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


def _parse_input_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class EntryHandle:
    upload_id: str
    entry_id: str


@dataclass(frozen=True)
class _Note:
    text: str
    label: str
    mainfile: str


@dataclass
class ExperimentSummary:
    upload_id: str
    entry_id: str
    name: str


@dataclass
class CollectedInput:
    entry_id: str
    kind: str  # 'audio' | 'note'
    text: str | None
    label: str
    datetime: str | None


_TRANSCRIPT_FIELDS = (
    'intended_transcript',
    'corrected_transcript',
    'whisper_transcript',
)


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
        self._writer = RawFileWriter(retry_interval_s, write_timeout_s)

    def build_client(self, token: str) -> httpx.AsyncClient:
        return build_client(self._base_url, token)

    def entry_url(self, upload_id: str, entry_id: str) -> str:
        return gui_entry_url(self._base_url, upload_id, entry_id)

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
            check_response(response, step='list_input_collections')
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
                name=entry.get('entry_name') or '',
            )
            for entry in entries
        ]

    async def create_input_collection(
        self, client: httpx.AsyncClient, name: str
    ) -> EntryHandle:
        upload_id = await create_upload(client, upload_name=name)
        await self._writer.write_archive(
            client,
            upload_id,
            EXPERIMENT_MAINFILE,
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
            entry_id=generate_entry_id(upload_id, EXPERIMENT_MAINFILE),
        )

    async def add_written_note(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        text: str,
        collection_entry_id: str,
    ) -> EntryHandle:
        """Add a typed step note (WrittenNote labeled 'step').

        collection_entry_id names the exact InputCollection entry the
        note is attached to (an upload can hold more than one).
        """
        mainfile = f'note_{_utc_now_stamp()}.archive.json'
        note = _Note(text=text, label=STEP_LABEL, mainfile=mainfile)
        return await self._add_note(client, upload_id, note, collection_entry_id)

    async def add_experiment_info(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        info_json: str,
        collection_entry_id: str,
    ) -> EntryHandle:
        """Store the experiment-info form JSON as its dedicated
        WrittenNote (label routing, see docs/handover.md §8)."""
        note = _Note(
            text=info_json,
            label=EXPERIMENT_INFO_LABEL,
            mainfile=EXPERIMENT_INFO_MAINFILE,
        )
        return await self._add_note(client, upload_id, note, collection_entry_id)

    async def _add_note(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        note: '_Note',
        collection_entry_id: str,
    ) -> EntryHandle:
        """Create the WrittenNote entry and reference it from the collection."""
        collection_mainfile = await self._resolve_collection_mainfile(
            client, upload_id, collection_entry_id
        )
        await self._writer.write_archive(
            client,
            upload_id,
            note.mainfile,
            {
                'data': {
                    'm_def': WRITTEN_NOTE_M_DEF,
                    'name': note.label,
                    'datetime': _utc_now_iso(),
                    'text': note.text,
                    'label': note.label,
                }
            },
        )
        handle = EntryHandle(
            upload_id=upload_id,
            entry_id=generate_entry_id(upload_id, note.mainfile),
        )
        await self._append_to_collection(
            client, upload_id, 'notes', handle.entry_id, collection_mainfile
        )
        return handle

    async def add_audio(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        audio: bytes,
        filename: str,
        collection_entry_id: str,
    ) -> EntryHandle:
        """Add a recording to an experiment.

        Uploads the audio file into the experiment upload (the voice-eln
        parser turns it into an AudioInput entry and transcribes it) and
        references the entry from the experiment's InputCollection.
        """
        # Fail before storing the audio if there is no collection to
        # reference it from; otherwise the file would sit orphaned in the
        # upload and every retry would deposit another copy.
        mainfile = await self._resolve_collection_mainfile(
            client, upload_id, collection_entry_id
        )
        await self._writer.read_archive(client, upload_id, mainfile)

        # Timestamp prefix: recordings all arrive as e.g. 'recording.webm',
        # and a second file with the same name would overwrite the first.
        stored_name = f'{_utc_now_stamp()}_{posixpath.basename(filename)}'
        await self._writer.upload_raw_file(
            client, upload_id, stored_name, audio, 'application/octet-stream'
        )

        # The parser creates the AudioInput entry under a deterministic
        # companion mainfile, so the entry id is known before the entry exists.
        entry_id = generate_entry_id(upload_id, f'{stored_name}.archive.json')
        await self._append_to_collection(
            client, upload_id, 'audios', entry_id, mainfile
        )
        return EntryHandle(upload_id=upload_id, entry_id=entry_id)

    async def add_derived_sheet(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        xlsx: bytes,
        sheet_mainfile: str,
        collection_entry_id: str,
    ) -> EntryHandle:
        """Store the derived experiment sheet and link it from the collection."""
        mainfile = await self._resolve_collection_mainfile(
            client, upload_id, collection_entry_id
        )
        await self._writer.read_archive(client, upload_id, mainfile)

        await self._writer.upload_raw_file(
            client,
            upload_id,
            sheet_mainfile,
            xlsx,
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        entry_id = generate_entry_id(upload_id, sheet_mainfile)
        await self._append_to_collection(
            client, upload_id, 'derived_entries', entry_id, mainfile
        )
        return EntryHandle(upload_id=upload_id, entry_id=entry_id)

    async def collect_inputs(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        collection_entry_id: str,
    ) -> list[CollectedInput]:
        """All inputs of an experiment's collection, as one ordered list.

        Ordered by the entry's datetime — user-editable in NOMAD, so
        researchers can correct or arrange the timeline. Entries without
        one sort last; ties break by entry id. Caveat: AudioInput
        datetimes are the NOMAD server's local time until
        nomad-voice-eln#41 is fixed.
        """
        mainfile = await self._resolve_collection_mainfile(
            client, upload_id, collection_entry_id
        )
        archive = await self._writer.read_archive(client, upload_id, mainfile)
        data = archive.get('data')
        if not isinstance(data, dict):
            raise NomadAPIError(
                0,
                f'Experiment mainfile {mainfile} has no data section',
                step='collect_inputs',
            )

        targets: list[tuple[str, str]] = []  # (entry_id, kind)
        seen: set[str] = set()
        for kind, field in (('audio', 'audios'), ('note', 'notes')):
            refs = data.get(field)
            for ref in refs if isinstance(refs, list) else []:
                entry_id = entry_id_from_ref(ref)
                if entry_id not in seen:
                    seen.add(entry_id)
                    targets.append((entry_id, kind))

        inputs = await asyncio.gather(
            *(self._fetch_input(client, entry_id, kind) for entry_id, kind in targets)
        )

        def sort_key(item: CollectedInput):
            parsed = _parse_input_datetime(item.datetime)
            return (
                parsed is None,
                parsed.timestamp() if parsed else 0.0,
                item.entry_id,
            )

        return sorted(inputs, key=sort_key)

    async def _fetch_input(
        self, client: httpx.AsyncClient, entry_id: str, kind: str
    ) -> CollectedInput:
        response = await client.get(f'/entries/{entry_id}/archive')
        if response.status_code == HTTPStatus.NOT_FOUND:
            return CollectedInput(
                entry_id=entry_id, kind=kind, text=None, label='', datetime=None
            )
        check_response(response, step='collect_inputs')
        try:
            body = response.json()
        except ValueError:
            body = {}
        # 'or {}' at every level: NOMAD serializes unset sections as null
        section = ((body.get('data') or {}).get('archive') or {}).get('data') or {}

        if kind == 'audio':
            text = next(
                (
                    str(section[field]).strip()
                    for field in _TRANSCRIPT_FIELDS
                    if section.get(field) and str(section[field]).strip()
                ),
                None,
            )
        else:
            raw = section.get('text')
            text = str(raw).strip() if raw and str(raw).strip() else None

        return CollectedInput(
            entry_id=entry_id,
            kind=kind,
            text=text,
            label=str(section.get('label') or ''),
            datetime=section.get('datetime'),
        )

    async def _append_to_collection(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        field: str,
        entry_id: str,
        mainfile: str,
    ) -> None:
        """Reference a new entry from the experiment's InputCollection.

        Read-modify-write of the collection mainfile; concurrent uploads to
        the same experiment can race here (accepted for now, see the design
        discussion).
        """
        archive = await self._writer.read_archive(client, upload_id, mainfile)
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
            await self._writer.write_archive(client, upload_id, mainfile, archive)

    async def _resolve_collection_mainfile(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        collection_entry_id: str,
    ) -> str:
        """return mainfile path"""
        if collection_entry_id == generate_entry_id(upload_id, EXPERIMENT_MAINFILE):
            return EXPERIMENT_MAINFILE
        response = await client.post(
            '/entries/query',
            json={
                'owner': 'visible',
                'query': {'entry_id': collection_entry_id, 'upload_id': upload_id},
                'required': {'include': ['mainfile']},
                'pagination': {'page_size': 1},
            },
        )
        check_response(response, step='find_collection')
        entries = response.json().get('data', [])
        if not entries:
            raise NomadAPIError(
                HTTPStatus.NOT_FOUND,
                f'No entry {collection_entry_id} found in upload {upload_id}',
                step='find_collection',
            )
        return entries[0]['mainfile']


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now_stamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
