import asyncio
import json
from datetime import datetime, timezone
from pathlib import PurePath
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request, Response, UploadFile
from nomad.utils import generate_entry_id

from sand import feedback
from sand.apis.deps import get_bearer_token
from sand.hysprint import EXPERIMENT_INFO_LABEL, EXPERIMENT_INFO_MAINFILE
from sand.hysprint.sheet import DERIVED_SHEET_MAINFILE, EXTRACTION_STATUS_MAINFILE
from sand.hysprint.sheet_store import SheetStore
from sand.models.input_collections import (
    ConfirmEditsRequest,
    ConfirmEditsResponse,
    CreateHysprintExperimentRequest,
    CreateNoteRequest,
    FeedbackRequest,
    FeedbackResponse,
    InputCollectionListResponse,
    InputCollectionResponse,
    InputCollectionSummaryModel,
    InputItemModel,
    InputListResponse,
    JobResponse,
    RenameRequest,
    ReviseDatetimeRequest,
    ReviseInputRequest,
    ReviseInputResponse,
    ReviseLabelRequest,
    SheetUploadResponse,
)
from sand.services.nomad_api import (
    XLSX_CONTENT_TYPE,
    NomadAPIError,
    NomadAuthError,
    check_response,
)
from sand.services.voice_eln import (
    AUDIO_EXTENSIONS,
    AudioUpload,
    VoiceElnService,
    normalize_audio_filename,
)
from sand.sheet_edit import catalog, changelog, view
from sand.sheet_edit.session import (
    CHANGELOG_FILE,
    CONFIRMATIONS_FILE,
    FINAL_FILE,
    ORIGINAL_FILE,
    STATUS_FILE,
    Confirmation,
    confirmations_json,
    load_confirmations,
    read_confirmations,
)
from sand.sheet_edit.workbook import ingest_problems, sha256

router = APIRouter()

# Keep in sync with MAX_UPLOAD_SIZE in apis/static/record.js.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

CLIENT_ERROR_STATUSES = (400, 404, 409)


def _undeletable_entry_ids(upload_id: str) -> tuple[str, ...]:
    # the experiment-info form: the hysprint extraction reads it
    return (generate_entry_id(upload_id, EXPERIMENT_INFO_MAINFILE),)


def _voice_service(request: Request) -> VoiceElnService:
    return request.app.state.voice_eln


def _http_error(exc: NomadAPIError) -> HTTPException:
    if isinstance(exc, NomadAuthError):
        return HTTPException(status_code=401, detail=_nomad_detail(exc))
    if exc.status_code in CLIENT_ERROR_STATUSES:
        return HTTPException(status_code=exc.status_code, detail=_nomad_detail(exc))
    return HTTPException(status_code=502, detail=str(exc))


def _nomad_detail(exc: NomadAPIError) -> str:
    """The human-readable message: NOMAD errors carry a raw JSON body."""
    try:
        body = json.loads(exc.detail)
    except ValueError:
        return exc.detail
    if isinstance(body, dict) and isinstance(body.get('detail'), str):
        return body['detail']
    return exc.detail


async def _read_upload(file: UploadFile) -> bytes:
    buf = bytearray()
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail='File too large (max 25 MB)')
    if not buf:
        raise HTTPException(status_code=400, detail='Uploaded file is empty')
    return bytes(buf)


# A crashed worker can leave a non-terminal status behind; after this long
# without an update the guard stops trusting it and allows a fresh start.
STALE_JOB_S = 30 * 60

# Serializes check-then-start per upload within this process, and the
# 'starting' reservation snapshot covers the window until the workflow's
# first own snapshot. A multi-process deployment could still race in the
# instant between two processes' checks - accepted for the lab scale.
_upload_locks: dict[str, asyncio.Lock] = {}


def _upload_lock(upload_id: str) -> asyncio.Lock:
    """For every read-then-write of one of the upload's files."""
    lock = _upload_locks.get(upload_id)
    if lock is None:
        lock = _upload_locks[upload_id] = asyncio.Lock()
    return lock


def _job_running(status: dict | None, collection_entry_id: str | None) -> bool:
    """NOMAD picks the workflow id, so a duplicate cannot be rejected
    there: the status file is the guard. None: any collection's job
    counts (the edit job's files are the upload's, not a collection's)."""
    if not status:
        return False
    if collection_entry_id and status.get('collection_entry_id') != collection_entry_id:
        return False
    if status.get('phase') in ('completed', 'failed'):
        return False
    updated = status.get('updated_at')
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(updated)
    except (TypeError, ValueError):
        return False
    return age.total_seconds() < STALE_JOB_S


def _reservation(collection_entry_id: str, phase: str, **fields) -> dict:
    """A status written by the API: visible to any check until the
    workflow's first own snapshot replaces it."""
    return {
        'job_id': 'starting',
        'phase': phase,
        'collection_entry_id': collection_entry_id,
        'updated_at': datetime.now(timezone.utc).isoformat(),
        **fields,
    }


async def _me(client) -> dict:
    me = await client.get('/users/me')
    check_response(me, step='whoami')
    return me.json()


async def _user_id(client) -> str:
    return (await _me(client))['user_id']


async def _start_action(action_id: str, data) -> str:
    # the async variant: the sync one blocks the API event loop while
    # it sets up its Mongo/Temporal infrastructure
    from nomad.actions.manager import start_action_async

    return await start_action_async(action_id=action_id, data=data)


async def _release_reservation(
    voice: VoiceElnService, token: str, upload_id: str, file: str, status: dict
) -> None:
    """A start that failed must not block retries until the staleness
    timeout; a NOMAD error here is secondary to the one being reported.
    Accepted: a start that raised after Temporal did create the workflow
    opens the guard while that job runs."""
    try:
        async with voice.build_client(token) as client:
            await voice.writer.write_json(client, upload_id, file, status)
    except NomadAPIError:
        pass


def _entry_response(voice: VoiceElnService, upload_id: str, entry_id: str) -> dict:
    return {
        'upload_id': upload_id,
        'entry_id': entry_id,
        'entry_url': voice.entry_url(upload_id, entry_id),
    }


@router.get('/input-collections', response_model=InputCollectionListResponse)
async def list_input_collections(request: Request) -> InputCollectionListResponse:
    """The user's unpublished experiments (InputCollection entries)."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    try:
        async with voice.build_client(token) as client:
            input_collections = await voice.list_input_collections(client)
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return InputCollectionListResponse(
        input_collections=[
            InputCollectionSummaryModel(
                name=e.name,
                created=e.created,
                **_entry_response(voice, e.upload_id, e.entry_id),
            )
            for e in input_collections
        ]
    )


@router.post('/input-collections', response_model=InputCollectionSummaryModel)
async def create_hysprint_input_collection(
    body: CreateHysprintExperimentRequest,
    request: Request,
) -> InputCollectionSummaryModel:
    """Create an experiment: a NOMAD upload with an InputCollection entry.

    With `info`, the experiment-info form is stored alongside as a
    WrittenNote labeled 'experiment_info' and referenced by the collection.
    """
    voice = _voice_service(request)
    token = get_bearer_token(request)

    info = body.info.model_dump(exclude_none=True) if body.info else None
    name = body.name or (body.info.default_name() if body.info else None)
    if not name:
        raise HTTPException(
            status_code=400, detail='Provide a name or the experiment info'
        )

    try:
        async with voice.build_client(token) as client:
            result = await voice.create_input_collection(client, name)
            if info:
                await voice.add_written_note(
                    client,
                    result.upload_id,
                    json.dumps(info),
                    collection_entry_id=result.entry_id,
                    label=EXPERIMENT_INFO_LABEL,
                    mainfile=EXPERIMENT_INFO_MAINFILE,
                )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return InputCollectionSummaryModel(
        name=name, **_entry_response(voice, result.upload_id, result.entry_id)
    )


@router.patch(
    '/input-collections/{upload_id}/name',
    response_model=InputCollectionSummaryModel,
    # the service does not read `created` back: left out, not null, so
    # the page keeps the list's
    response_model_exclude_none=True,
)
async def rename_input_collection(
    upload_id: str,
    body: RenameRequest,
    request: Request,
    collection_entry_id: str,
) -> InputCollectionSummaryModel:
    """Rename a sheet: the collection's name, which the list shows,
    and the upload's name in NOMAD. Blank is refused. The answer has no
    `created`: the page keeps the list's."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    name = ' '.join(body.name.split())
    if not name:
        raise HTTPException(status_code=400, detail='Give the sheet a name')

    try:
        async with voice.build_client(token) as client:
            renamed = await voice.rename_input_collection(
                client, upload_id, collection_entry_id, name
            )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return InputCollectionSummaryModel(
        name=renamed.name,
        created=renamed.created,
        **_entry_response(voice, upload_id, collection_entry_id),
    )


@router.post(
    '/input-collections/from-sheet',
    response_model=InputCollectionSummaryModel,
    status_code=201,
)
async def create_sheet_input_collection(
    file: UploadFile,
    request: Request,
    *,
    name: str = Form(''),
) -> InputCollectionSummaryModel:
    """A sheet's collection: the uploaded lab sheet is the edit job's
    original, stored unchanged.

    A sheet the ingest checks refuse is refused before anything is
    created; the problems are the user's to fix and come back verbatim
    under `detail.problems`. The name defaults to the file's.
    """
    voice = _voice_service(request)
    token = get_bearer_token(request)

    xlsx = await _read_upload(file)
    # openpyxl work in a thread: a large sheet must not stall the API
    problems = await asyncio.to_thread(ingest_problems, xlsx)
    if problems:
        raise HTTPException(status_code=422, detail={'problems': problems})
    file_name = PurePath(file.filename or '').name
    name = name.strip() or PurePath(file_name).stem or 'sheet'

    try:
        async with voice.build_client(token) as client:
            # the file's name is the original's download name later
            result = await voice.create_input_collection(
                client, name, description=file_name
            )
            try:
                await voice.writer.upload_raw_file(
                    client, result.upload_id, ORIGINAL_FILE, xlsx, XLSX_CONTENT_TYPE
                )
            except NomadAPIError as exc:
                # without its sheet the collection's edit job can only
                # fail: take it back
                try:
                    await voice.delete_input_collection(client, result.upload_id)
                except NomadAPIError:
                    raise HTTPException(
                        status_code=502,
                        detail=f'{_nomad_detail(exc)}; the half-made sheet '
                        f'{result.upload_id} could not be removed either',
                    ) from exc
                raise
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return InputCollectionSummaryModel(
        name=name,
        # NOMAD stamps the entry a moment later; the list shows this one
        created=datetime.now(timezone.utc).isoformat(),
        **_entry_response(voice, result.upload_id, result.entry_id),
    )


@router.get('/input-collections/{upload_id}/sheet-view')
async def sheet_view(
    upload_id: str,
    request: Request,
    collection_entry_id: str,
) -> dict:
    """The sheet as the page shows it (`sheet_edit.view.sheet_view`, plus
    `source` and `sha256` of the file it was built from). Built from the
    edited sheet when the stored sheet and changelog hold together with
    the original by their hashes and the job that wrote them was this
    collection's; from the uploaded sheet otherwise, with a warning
    saying why. Reads do not wait for processing: the page polls this."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    try:
        async with voice.build_client(token) as client:
            original = await voice.writer.read_raw_file(
                client, upload_id, ORIGINAL_FILE, wait=False
            )
            if original is None:
                raise HTTPException(
                    status_code=404, detail='this sheet was not uploaded here'
                )
            final = await voice.writer.read_raw_file(
                client, upload_id, FINAL_FILE, wait=False
            )
            raw_log = status = None
            if final is not None:
                raw_log = await voice.writer.read_raw_file(
                    client, upload_id, CHANGELOG_FILE, wait=False
                )
                status = await voice.writer.read_json(
                    client, upload_id, STATUS_FILE, wait=False
                )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    sheet, log = _current_sheet(original, final, raw_log)
    warning = None
    if final is not None and log is None:
        warning = (
            'an edit job is writing the sheet; this is the uploaded sheet'
            if _job_running(status, None)
            else 'the edited sheet stored with this upload does not match '
            'its changelog; this is the uploaded sheet'
        )
    elif log is not None and (
        status is not None and status.get('collection_entry_id') != collection_entry_id
    ):
        # the sheet files are the upload's; the edits came from another
        # collection's updates, so they are not this sheet's
        sheet, log = original, None
        warning = (
            "another sheet's job wrote this upload's edited sheet; "
            'this is the uploaded sheet'
        )
    built = await asyncio.to_thread(lambda: view.sheet_view(catalog.build(sheet), log))
    if warning:
        built['warnings'].append(warning)
    return {
        'source': 'uploaded' if log is None else 'edited',
        'sha256': sha256(sheet),
        **built,
    }


def _current_sheet(
    original: bytes, final: bytes | None, raw_log: bytes | None
) -> tuple[bytes, changelog.Changelog | None]:
    """The edited sheet with its log when the three files are one
    another's (the log names both by hash), else the original alone."""
    if final is None or raw_log is None:
        return original, None
    try:
        log = changelog.from_json(raw_log.decode())
    except (UnicodeDecodeError, changelog.CheckFailed):
        return original, None
    if (log.original, log.final) != (sha256(original), sha256(final)):
        return original, None
    return final, log


@router.post(
    '/input-collections/{upload_id}/audio', response_model=InputCollectionResponse
)
async def add_audio(  # noqa: PLR0913
    upload_id: str,
    file: UploadFile,
    request: Request,
    collection_entry_id: str,
    *,
    transcript: str | None = Form(None),
    label: str = Form(''),
) -> InputCollectionResponse:
    """Add audio to an InputCollection entry, optionally labeled.

    collection_entry_id names the target collection exactly (an upload
    can hold more than one). `transcript` carries the live transcription
    result when the user chose to store it (the GUI toggle, defaulting
    to the store_live_transcript config): the AudioInput is then created
    pre-transcribed and whisper is skipped. Absent -> the live text was
    display-only and whisper transcribes the audio (issue #47).
    """
    voice = _voice_service(request)
    token = get_bearer_token(request)

    filename = normalize_audio_filename(file.filename or 'audio.m4a')
    if filename is None:
        raise HTTPException(
            status_code=415,
            detail='Unsupported audio format; use one of: '
            + ', '.join(sorted(AUDIO_EXTENSIONS)),
        )

    audio = await _read_upload(file)

    try:
        async with voice.build_client(token) as client:
            result = await voice.add_audio(
                client,
                upload_id,
                AudioUpload(
                    audio=audio,
                    filename=filename,
                    transcript=transcript,
                    stt_model=f'deepgram/{request.app.state.deepgram_model}',
                    label=label,
                ),
                collection_entry_id=collection_entry_id,
            )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return InputCollectionResponse(
        **_entry_response(voice, result.upload_id, result.entry_id)
    )


@router.post(
    '/input-collections/{upload_id}/notes', response_model=InputCollectionResponse
)
async def add_note(
    upload_id: str,
    body: CreateNoteRequest,
    request: Request,
    collection_entry_id: str,
) -> InputCollectionResponse:
    """Add a typed note (WrittenNote, optionally labeled) to the experiment.

    collection_entry_id names the target collection exactly (an upload
    can hold more than one).
    """
    voice = _voice_service(request)
    token = get_bearer_token(request)

    if not body.text.strip():
        raise HTTPException(status_code=400, detail='Note text is empty')

    try:
        async with voice.build_client(token) as client:
            result = await voice.add_written_note(
                client,
                upload_id,
                body.text,
                collection_entry_id=collection_entry_id,
                label=body.label.strip(),
            )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return InputCollectionResponse(
        **_entry_response(voice, result.upload_id, result.entry_id)
    )


@router.get('/input-collections/{upload_id}/inputs', response_model=InputListResponse)
async def list_inputs(
    upload_id: str,
    request: Request,
    collection_entry_id: str,
) -> InputListResponse:
    """All of the experiment's inputs in extraction order, including the
    experiment_info form note."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    try:
        async with voice.build_client(token) as client:
            inputs = await voice.collect_inputs(
                client, upload_id, collection_entry_id=collection_entry_id
            )
            locked = await _locked_inputs(client, voice, upload_id)
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    undeletable = _undeletable_entry_ids(upload_id)
    return InputListResponse(
        inputs=[
            InputItemModel(
                entry_id=item.entry_id,
                entry_url=voice.entry_url(upload_id, item.entry_id),
                kind=item.kind,
                label=item.label,
                datetime=item.datetime,
                text=item.text,
                corrected=item.corrected,
                status=item.status,
                deletable=item.entry_id not in undeletable,
                locked=item.entry_id in locked,
            )
            for item in inputs
        ]
    )


@router.post(
    '/input-collections/{upload_id}/inputs/{entry_id}/text',
    response_model=ReviseInputResponse,
)
async def revise_input(
    upload_id: str,
    entry_id: str,
    body: ReviseInputRequest,
    request: Request,
    collection_entry_id: str,
) -> ReviseInputResponse:
    """Save a human revision of one input.

    Audio -> corrected_transcript (empty text withdraws the correction,
    the machine transcript stays); note -> the text itself (empty is
    rejected). Only entries referenced by the collection are revisable.
    """
    voice = _voice_service(request)
    token = get_bearer_token(request)

    try:
        async with _upload_lock(upload_id), voice.build_client(token) as client:
            if await _confirmed_input(
                client, voice, upload_id, collection_entry_id, entry_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail='a confirmed update is a record and cannot be changed; '
                    'add a new update instead',
                )
            kind = await voice.revise_input(
                client,
                upload_id,
                entry_id,
                body.text,
                collection_entry_id=collection_entry_id,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return ReviseInputResponse(kind=kind)


@router.post(
    '/input-collections/{upload_id}/inputs/{entry_id}/datetime',
    response_model=ReviseInputResponse,
)
async def revise_input_datetime(
    upload_id: str,
    entry_id: str,
    body: ReviseDatetimeRequest,
    request: Request,
    collection_entry_id: str,
) -> ReviseInputResponse:
    """Set the datetime of one input, reordering it on the timeline.

    Not for an experiment that started from a sheet: there the time is
    when the update was made, and the order of updates is the record."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    try:
        async with voice.build_client(token) as client:
            if await voice.writer.read_raw_file(client, upload_id, ORIGINAL_FILE):
                raise HTTPException(
                    status_code=409,
                    detail='the time of an update is when it was made; '
                    'it cannot be changed',
                )
            kind = await voice.revise_input_datetime(
                client,
                upload_id,
                entry_id,
                body.datetime,
                collection_entry_id=collection_entry_id,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return ReviseInputResponse(kind=kind)


@router.post(
    '/input-collections/{upload_id}/inputs/{entry_id}/label',
    response_model=ReviseInputResponse,
)
async def revise_input_label(
    upload_id: str,
    entry_id: str,
    body: ReviseLabelRequest,
    request: Request,
    collection_entry_id: str,
) -> ReviseInputResponse:
    """Set the free-text label of one input; empty clears it."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    try:
        async with voice.build_client(token) as client:
            kind = await voice.revise_input_label(
                client,
                upload_id,
                entry_id,
                body.label,
                collection_entry_id=collection_entry_id,
            )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return ReviseInputResponse(kind=kind)


@router.delete('/input-collections/{upload_id}/inputs/{entry_id}', status_code=204)
async def delete_input(
    upload_id: str,
    entry_id: str,
    request: Request,
    collection_entry_id: str,
) -> Response:
    """Delete one input: its entry, a recording's audio, and the
    collection's reference to it."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    try:
        async with _upload_lock(upload_id), voice.build_client(token) as client:
            if await _confirmed_input(
                client, voice, upload_id, collection_entry_id, entry_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail='a confirmed update is a record and cannot be deleted; '
                    'add a new update that sets the value back',
                )
            await voice.delete_input(
                client,
                upload_id,
                entry_id,
                collection_entry_id=collection_entry_id,
                undeletable=_undeletable_entry_ids(upload_id),
            )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return Response(status_code=204)


@router.get('/input-collections/{upload_id}/sheet')
async def download_sheet(
    upload_id: str,
    request: Request,
    collection_entry_id: str,
) -> Response:
    """The derived experiment sheet, as an xlsx download."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    try:
        async with voice.build_client(token) as client:
            xlsx = await SheetStore(voice).read(client, upload_id, collection_entry_id)
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    if xlsx is None:
        raise HTTPException(
            status_code=404,
            detail='No sheet in this experiment yet; extract first',
        )
    return Response(
        content=xlsx,
        media_type=XLSX_CONTENT_TYPE,
        headers={
            'Content-Disposition': f'attachment; filename="{DERIVED_SHEET_MAINFILE}"'
        },
    )


@router.put('/input-collections/{upload_id}/sheet', response_model=SheetUploadResponse)
async def upload_sheet(
    upload_id: str,
    file: UploadFile,
    request: Request,
    collection_entry_id: str,
) -> SheetUploadResponse:
    """Replace the derived sheet with a user-uploaded one.

    Stored under the fixed sheet name.
    The extraction json is never touched by user replace the xlsx.
    By comparing the hash in extraction json and the current xlsx,
    we know if the xlsx is user edited or directly converted from
    extracted json.
    """
    voice = _voice_service(request)
    token = get_bearer_token(request)

    if not (file.filename or '').lower().endswith('.xlsx'):
        raise HTTPException(status_code=415, detail='Upload an .xlsx file')
    xlsx = await _read_upload(file)

    try:
        async with voice.build_client(token) as client:
            handle, changed = await SheetStore(voice).store_uploaded(
                client, upload_id, xlsx, collection_entry_id
            )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return SheetUploadResponse(
        changed=changed,
        derived_entry=InputCollectionResponse(
            **_entry_response(voice, handle.upload_id, handle.entry_id)
        ),
    )


@router.post(
    '/input-collections/{upload_id}/extract-async',
    response_model=JobResponse,
)
async def start_extract_async(
    upload_id: str,
    request: Request,
    collection_entry_id: str,
) -> JobResponse:
    """Start the extraction action; poll /extract-status for progress."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    async with _upload_lock(upload_id):
        try:
            async with voice.build_client(token) as client:
                status = await voice.writer.read_json(
                    client, upload_id, EXTRACTION_STATUS_MAINFILE
                )
                if _job_running(status, collection_entry_id):
                    raise HTTPException(
                        status_code=409,
                        detail='an extraction is already running for this '
                        'experiment; wait for it to finish',
                    )
                # validates the collection (and the token) before starting
                await voice.collect_inputs(
                    client, upload_id, collection_entry_id=collection_entry_id
                )
                user_id = await _user_id(client)
                await voice.writer.write_json(
                    client,
                    upload_id,
                    EXTRACTION_STATUS_MAINFILE,
                    _reservation(collection_entry_id, 'starting', steps=[]),
                )
        except NomadAPIError as exc:
            raise _http_error(exc) from exc

        from sand.actions.extract.models import ExtractInput

        try:
            job_id = await _start_action(
                'sand.actions.extract:extract_action_entry_point',
                ExtractInput(
                    upload_id=upload_id,
                    user_id=user_id,
                    collection_entry_id=collection_entry_id,
                ),
            )
        except Exception as exc:
            error = f'could not start the extraction: {exc}'
            await _release_reservation(
                voice,
                token,
                upload_id,
                EXTRACTION_STATUS_MAINFILE,
                _reservation(collection_entry_id, 'failed', error=error, steps=[]),
            )
            raise HTTPException(status_code=502, detail=error) from exc
    return JobResponse(job_id=job_id)


@router.get('/input-collections/{upload_id}/extract-status')
async def extract_status(
    upload_id: str,
    request: Request,
    collection_entry_id: str,
) -> dict:
    """The asynchronous extraction's progress, read from the status file
    the workflow writes into the upload."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    try:
        async with voice.build_client(token) as client:
            status = await voice.writer.read_json(
                client, upload_id, EXTRACTION_STATUS_MAINFILE
            )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    # the status file is upload-level and an upload can hold several
    # collections: only report a status belonging to the requested one
    if status is None or status.get('collection_entry_id') != collection_entry_id:
        raise HTTPException(
            status_code=404, detail='no extraction status for this collection yet'
        )
    return status


@router.post('/input-collections/{upload_id}/edit-async', response_model=JobResponse)
async def start_edit_async(
    upload_id: str,
    request: Request,
    collection_entry_id: str,
) -> JobResponse:
    """Start the edit job: every input interpreted against the uploaded
    sheet, the confirmed edits written. Poll /edit-status for progress."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    async with _upload_lock(upload_id):
        try:
            async with voice.build_client(token) as client:
                status = await voice.writer.read_json(client, upload_id, STATUS_FILE)
                if _job_running(status, None):
                    raise HTTPException(
                        status_code=409,
                        detail='an edit job is already running on this sheet; '
                        'wait for it to finish',
                    )
                original = await voice.writer.read_raw_file(
                    client, upload_id, ORIGINAL_FILE
                )
                if original is None:
                    raise HTTPException(
                        status_code=422,
                        detail='no sheet was uploaded here',
                    )
                inputs = await voice.collect_inputs(
                    client, upload_id, collection_entry_id=collection_entry_id
                )
                if not inputs:
                    raise HTTPException(
                        status_code=422, detail='nothing to apply: add an update first'
                    )
                pending = [i for i in inputs if i.text is None]
                if pending:
                    raise HTTPException(
                        status_code=422,
                        detail=f'{len(pending)} input(s) not transcribed or '
                        'processed yet; try again in a moment',
                    )
                user_id = await _user_id(client)
                await voice.writer.write_json(
                    client,
                    upload_id,
                    STATUS_FILE,
                    _reservation(collection_entry_id, 'starting', inputs=[]),
                )
        except NomadAPIError as exc:
            raise _http_error(exc) from exc

        from sand.actions.edit.models import EditInput

        try:
            job_id = await _start_action(
                'sand.actions.edit:edit_action_entry_point',
                EditInput(
                    upload_id=upload_id,
                    user_id=user_id,
                    collection_entry_id=collection_entry_id,
                ),
            )
        except Exception as exc:
            error = f'could not start the edit job: {exc}'
            await _release_reservation(
                voice,
                token,
                upload_id,
                STATUS_FILE,
                _reservation(collection_entry_id, 'failed', error=error, inputs=[]),
            )
            raise HTTPException(status_code=502, detail=error) from exc
    return JobResponse(job_id=job_id)


async def _edit_status(
    client, voice: VoiceElnService, upload_id: str, collection_entry_id: str
) -> dict:
    status = await voice.writer.read_json(client, upload_id, STATUS_FILE)
    # the status file is upload-level and an upload can hold several
    # collections: only report a status belonging to the requested one
    if status is None or status.get('collection_entry_id') != collection_entry_id:
        raise HTTPException(status_code=404, detail='no edit job for this sheet yet')
    if status.get('phase') not in ('completed', 'failed') and not _job_running(
        status, collection_entry_id
    ):
        # a job that stopped answering (a crashed worker, a reservation
        # whose start never reached Temporal): the page would wait on
        # it forever, so it is reported as the failure it is
        status = {
            **status,
            'phase': 'failed',
            'stale': True,
            'error': 'the job stopped: no progress for '
            f'{STALE_JOB_S // 60} minutes; read the updates again',
        }
    confirmed = await voice.writer.read_json(client, upload_id, CONFIRMATIONS_FILE)
    return _with_confirmations(status, confirmed or {})


async def _locked_inputs(client, voice: VoiceElnService, upload_id: str) -> set[str]:
    """The inputs (entry ids) named by a confirmation: their words are a
    record. A damaged file is read as nothing locked here (a listing);
    the endpoints that change an input refuse on it."""
    raw = await voice.writer.read_raw_file(client, upload_id, CONFIRMATIONS_FILE)
    try:
        confirmed = load_confirmations(read_confirmations(raw))
    except ValueError:
        return set()
    return {c.input for c in confirmed.values() if c.input is not None}


async def _confirmed_input(
    client,
    voice: VoiceElnService,
    upload_id: str,
    collection_entry_id: str,
    entry_id: str,
) -> bool:
    """Whether an edit of this input was ever confirmed. Its words are a
    record then: the sheet's history is appended to, never rewritten.
    The confirmations name their input, so the lock outlives the key
    (which changes with the sheet state before the input) and the status
    file (another collection's job may replace it). A damaged file locks
    everything: it needs a look, like the confirm endpoint says."""
    raw = await voice.writer.read_raw_file(client, upload_id, CONFIRMATIONS_FILE)
    try:
        confirmed = load_confirmations(read_confirmations(raw))
    except ValueError as why:
        raise HTTPException(
            status_code=409, detail=f'confirmations file damaged: {why}'
        ) from why
    if any(c.input == entry_id for c in confirmed.values()):
        return True
    # records made before confirmations named their input: by key
    status = await voice.writer.read_json(client, upload_id, STATUS_FILE)
    if status is None or status.get('collection_entry_id') != collection_entry_id:
        return False
    overlaid = _with_confirmations(status, confirmations_json(confirmed))
    for report in overlaid.get('inputs', []):
        if isinstance(report, dict) and report.get('entry_id') == entry_id:
            return bool(report.get('confirmed'))
    return False
    confirmed = await voice.writer.read_json(client, upload_id, CONFIRMATIONS_FILE)
    for report in _with_confirmations(status, confirmed or {}).get('inputs', []):
        if isinstance(report, dict) and report.get('entry_id') == entry_id:
            return bool(report.get('confirmed'))
    return False


def _with_confirmations(status: dict, confirmed: dict) -> dict:
    """The reports say what the job knew when it ran; a confirmation made
    since shows at once, as `confirmed_since`: it waits for the next run."""
    for report in status.get('inputs', []):
        if not isinstance(report, dict):
            continue
        known = set(report.get('confirmed', []))
        since = [
            edit['key']
            for edit in report.get('ready', [])
            if edit['key'] in confirmed and edit['key'] not in known
        ]
        report['confirmed'] = [*report.get('confirmed', []), *since]
        report['confirmed_since'] = since
    return status


async def _completed_status(
    client, voice: VoiceElnService, upload_id: str, collection_entry_id: str
) -> dict:
    """The status of the last job, which must have completed: what the
    final sheet and the changelog are checked against."""
    status = await _edit_status(client, voice, upload_id, collection_entry_id)
    if _job_running(status, None):
        raise HTTPException(
            status_code=409, detail='an edit job is running; wait for it'
        )
    if status.get('phase') != 'completed':
        raise HTTPException(
            status_code=409, detail='the last edit job did not complete; run it again'
        )
    return status


async def _read_edit_status(
    voice: VoiceElnService, token: str, upload_id: str, collection_entry_id: str
) -> dict:
    try:
        async with voice.build_client(token) as client:
            return await _edit_status(client, voice, upload_id, collection_entry_id)
    except NomadAPIError as exc:
        raise _http_error(exc) from exc


@router.get('/input-collections/{upload_id}/edit-status')
async def edit_status(
    upload_id: str,
    request: Request,
    collection_entry_id: str,
) -> dict:
    """The edit job's progress: the status file the workflow writes."""
    return await _read_edit_status(
        _voice_service(request),
        get_bearer_token(request),
        upload_id,
        collection_entry_id,
    )


@router.get('/input-collections/{upload_id}/edits')
async def edit_reports(
    upload_id: str,
    request: Request,
    collection_entry_id: str,
) -> dict:
    """What the job made of each input, in input order: text, preview,
    the edits ready to confirm (with their keys), what was written or
    refused. Partial while the phase is not completed."""
    status = await _read_edit_status(
        _voice_service(request),
        get_bearer_token(request),
        upload_id,
        collection_entry_id,
    )
    return {
        'job_id': status.get('job_id'),
        'phase': status.get('phase'),
        'inputs': status.get('inputs', []),
    }


@router.post(
    '/input-collections/{upload_id}/edits/confirm', response_model=ConfirmEditsResponse
)
async def confirm_edits(
    upload_id: str,
    body: ConfirmEditsRequest,
    request: Request,
    collection_entry_id: str,
) -> ConfirmEditsResponse:
    """Confirm ready edits by their keys. Only recorded here; the next
    job run writes them into the sheet (its answers are cached, so the
    run costs no model calls). A key must be a ready edit of the last
    job's reports. An already confirmed key keeps its first record."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    if not body.keys:
        raise HTTPException(status_code=400, detail='no keys to confirm')
    try:
        async with _upload_lock(upload_id), voice.build_client(token) as client:
            status = await _edit_status(client, voice, upload_id, collection_entry_id)
            reports = {
                edit['key']: report
                for report in status.get('inputs', [])
                for edit in report.get('ready', [])
            }
            unknown = sorted(set(body.keys) - set(reports))
            if unknown and _job_running(status, None):
                # the running job's reports replace the old ones as it goes
                raise HTTPException(
                    status_code=409,
                    detail='an edit job is running; confirm when it has finished',
                )
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=f'not a ready edit of the last job: {", ".join(unknown)}',
                )
            # a confirmation locks the input's words for good: they must be
            # the words the report read, not a revision made since
            inputs = await voice.collect_inputs(
                client, upload_id, collection_entry_id=collection_entry_id
            )
            texts = {i.entry_id: i.text for i in inputs}
            changed = sorted(
                {
                    reports[key].get('entry_id')
                    for key in body.keys
                    if texts.get(reports[key].get('entry_id'))
                    != reports[key].get('text')
                }
            )
            if changed:
                raise HTTPException(
                    status_code=409,
                    detail='the words of an input changed since the last run; '
                    'apply the updates again, then confirm',
                )
            raw = await voice.writer.read_raw_file(
                client, upload_id, CONFIRMATIONS_FILE
            )
            try:
                confirmed = load_confirmations(read_confirmations(raw))
            except ValueError as why:
                # an audit file is never overwritten; this needs a look
                raise HTTPException(
                    status_code=409, detail=f'confirmations file damaged: {why}'
                ) from why
            me = await _me(client)
            by = me.get('username') or me.get('name') or me['user_id']
            now = datetime.now(timezone.utc)
            for key in body.keys:
                confirmed.setdefault(
                    key,
                    Confirmation(by=by, at=now, input=reports[key].get('entry_id')),
                )
            await voice.writer.write_json(
                client, upload_id, CONFIRMATIONS_FILE, confirmations_json(confirmed)
            )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return ConfirmEditsResponse(confirmed=sorted(confirmed))


@router.get('/input-collections/{upload_id}/original-sheet')
async def download_original_sheet(
    upload_id: str,
    request: Request,
    collection_entry_id: str,
) -> Response:
    """The sheet as uploaded, under the uploaded file's name (kept in the
    collection's description); under the sheet's name with
    "(original)" when that is gone or not a file name any more. Both
    were written at the upload: read at once, a running job's writes
    must not hold this up."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    try:
        async with voice.build_client(token) as client:
            sheet = await voice.writer.read_raw_file(
                client, upload_id, ORIGINAL_FILE, wait=False
            )
            if sheet is None:
                raise HTTPException(
                    status_code=404,
                    detail='no sheet was uploaded here',
                )
            file_name = await voice.input_collection_description(
                client, upload_id, collection_entry_id, wait=False
            )
            if not file_name.lower().endswith('.xlsx'):
                name = await voice.input_collection_name(
                    client, upload_id, collection_entry_id
                )
                file_name = f'{name} (original).xlsx'
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return Response(
        content=sheet,
        media_type=XLSX_CONTENT_TYPE,
        headers={'Content-Disposition': _attachment(file_name)},
    )


@router.get('/input-collections/{upload_id}/final-sheet')
async def download_final_sheet(
    upload_id: str,
    request: Request,
    collection_entry_id: str,
) -> Response:
    """The sheet as it stands, as an xlsx download under the sheet's
    name: the one the last completed job left (the original plus
    the confirmed edits), or the uploaded one while no job has written
    a sheet."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    try:
        async with voice.build_client(token) as client:
            status = await voice.writer.read_json(client, upload_id, STATUS_FILE)
            if status is not None and (
                status.get('collection_entry_id') != collection_entry_id
            ):
                # the upload's sheet files are another collection's now
                raise HTTPException(
                    status_code=409,
                    detail="another sheet's job wrote this upload's sheet; "
                    'run the job again',
                )
            if status is not None and _job_running(status, None):
                raise HTTPException(
                    status_code=409, detail='an edit job is running; wait for it'
                )
            final = (
                await voice.writer.read_raw_file(client, upload_id, FINAL_FILE)
                if status is not None
                else None
            )
            if status is not None and status.get('phase') == 'completed':
                if final is None:
                    raise HTTPException(status_code=404, detail='no final sheet stored')
                if sha256(final) != status.get('final'):
                    # a file touched by hand
                    raise HTTPException(
                        status_code=409,
                        detail='the stored sheet is not the one the last job '
                        'recorded; run the job again',
                    )
                sheet = final
            elif final is not None:
                # a failed job after one that wrote: the stored sheet is the
                # earlier job's, and this status cannot vouch for it
                raise HTTPException(
                    status_code=409,
                    detail='the last edit job did not complete; run it again',
                )
            else:
                # no job yet, or none that wrote a sheet
                sheet = await voice.writer.read_raw_file(
                    client, upload_id, ORIGINAL_FILE
                )
                if sheet is None:
                    raise HTTPException(
                        status_code=404,
                        detail='no sheet was uploaded here',
                    )
            name = await voice.input_collection_name(
                client, upload_id, collection_entry_id
            )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return Response(
        content=sheet,
        media_type=XLSX_CONTENT_TYPE,
        headers={'Content-Disposition': _attachment(f'{name}.xlsx')},
    )


def _attachment(filename: str) -> str:
    """Headers are latin-1: an ASCII fallback plus the real name encoded
    (RFC 6266), so a sheet named in any script downloads."""
    ascii_name = ''.join(
        c if c.isascii() and c.isprintable() and c not in '"\\' else '_'
        for c in filename
    )
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename, safe="")}'


@router.get('/input-collections/{upload_id}/changelog')
async def read_changelog(
    upload_id: str,
    request: Request,
    collection_entry_id: str,
    format: str = 'text',  # noqa: A002
) -> Response:
    """What the final sheet changed against the original, cell by cell,
    with who confirmed each edit and when: rendered text, or the stored
    JSON (`format=json`). Only an intact log of the last job is served."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    if format not in ('text', 'json'):
        raise HTTPException(status_code=400, detail='format is text or json')
    try:
        async with voice.build_client(token) as client:
            status = await _completed_status(
                client, voice, upload_id, collection_entry_id
            )
            raw = await voice.writer.read_raw_file(client, upload_id, CHANGELOG_FILE)
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    if raw is None:
        raise HTTPException(status_code=404, detail='no changelog stored')
    try:
        log = changelog.from_json(raw.decode())
    except (UnicodeDecodeError, changelog.CheckFailed) as why:
        raise HTTPException(
            status_code=409, detail=f'changelog damaged: {why}'
        ) from why
    if log.final != status.get('final'):
        raise HTTPException(
            status_code=409,
            detail="the stored changelog is not the last job's; run the job again",
        )
    if format == 'json':
        return Response(content=raw, media_type='application/json')
    return Response(content=changelog.render(log), media_type='text/plain')


@router.post(
    '/input-collections/{upload_id}/feedback',
    status_code=201,
    response_model=FeedbackResponse,
)
async def send_feedback(
    upload_id: str,
    request: Request,
    collection_entry_id: str,
    body: FeedbackRequest,
) -> FeedbackResponse:
    """Store a "Something's wrong" report in the sheet's upload, with a
    snapshot of what the user saw."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    at = datetime.now(timezone.utc)
    try:
        async with voice.build_client(token) as client:
            me = await _me(client)
            name = inputs = status = confirmed = context_error = None
            try:
                name = await voice.input_collection_name(
                    client, upload_id, collection_entry_id
                )
                inputs = await voice.collect_inputs(
                    client, upload_id, collection_entry_id
                )
                status = await voice.writer.read_json(
                    client, upload_id, STATUS_FILE, wait=False
                )
                # upload-level: it may hold another collection's job
                if status and status.get('collection_entry_id') != collection_entry_id:
                    status = None
                confirmed = await voice.writer.read_json(
                    client, upload_id, CONFIRMATIONS_FILE, wait=False
                )
            except NomadAPIError as exc:
                context_error = str(exc)
            report = feedback.report(
                at=at,
                by=me.get('username') or me.get('name') or me['user_id'],
                what_wrong=body.what_wrong,
                expected=body.expected,
                page=body.page,
                client={
                    'user_agent': body.user_agent,
                    'recent_errors': body.recent_errors,
                },
                sheet={
                    'upload_id': upload_id,
                    'collection_entry_id': collection_entry_id,
                    'name': name,
                },
                inputs=inputs,
                status=status,
                confirmations=confirmed,
                context_error=context_error,
            )
            file = feedback.report_file(at)
            await voice.writer.write_json(client, upload_id, file, report)
    except NomadAPIError as exc:
        raise _http_error(exc) from exc
    return FeedbackResponse(file=file)
