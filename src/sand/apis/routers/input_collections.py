import asyncio
import json
from datetime import datetime, timezone
from pathlib import PurePath
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request, Response, UploadFile

from sand import feedback
from sand.apis.deps import get_bearer_token
from sand.models.input_collections import (
    ConfirmEditsRequest,
    ConfirmEditsResponse,
    CreateNoteRequest,
    DecisionsRequest,
    FeedbackRequest,
    FeedbackResponse,
    InputCollectionListResponse,
    InputCollectionResponse,
    InputCollectionSummaryModel,
    InputItemModel,
    InputListResponse,
    JobResponse,
    RejectEditsRequest,
    RejectEditsResponse,
    RenameRequest,
    ReviseInputRequest,
    ReviseInputResponse,
    ReviseLabelRequest,
    UndoneResponse,
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
    REJECTIONS_FILE,
    STATUS_FILE,
    Confirmation,
    confirmations_json,
    load_confirmations,
    read_confirmations,
    read_rejections,
)
from sand.sheet_edit.workbook import ingest_problems, sha256

router = APIRouter()

# Keep in sync with MAX_UPLOAD_SIZE in apis/static/record.js.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

CLIENT_ERROR_STATUSES = (400, 404, 409)


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


def _job_running(status: dict | None) -> bool:
    """NOMAD picks the workflow id, so a duplicate cannot be rejected
    there: the status file is the guard. Any collection's job counts: the
    edit job's files are the upload's, not a collection's."""
    if not status:
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
            if _job_running(status)
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
    """All of the experiment's inputs, in order."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    try:
        async with voice.build_client(token) as client:
            inputs = await voice.collect_inputs(
                client, upload_id, collection_entry_id=collection_entry_id
            )
            applied = await _applied_inputs(
                client, voice, upload_id, collection_entry_id
            )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

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
                applied=item.entry_id in applied,
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
            # first: a revision saved with its old confirmations kept would
            # count as confirmed again if its words were changed back; not
            # for an input the revision is refused on (an empty note)
            item = await _input_of(
                client, voice, upload_id, collection_entry_id, entry_id
            )
            if item is not None and (body.text.strip() or item.kind == 'audio'):
                await _drop_decisions(client, voice, upload_id, entry_id)
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
            item = await _input_of(
                client, voice, upload_id, collection_entry_id, entry_id
            )
            if item is not None:
                await _drop_decisions(client, voice, upload_id, entry_id)
            await voice.delete_input(
                client,
                upload_id,
                entry_id,
                collection_entry_id=collection_entry_id,
            )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return Response(status_code=204)


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
                if _job_running(status):
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
                # with every update deleted, a job still writes the
                # original back over the last job's sheet
                read_before = (
                    status is not None
                    and status.get('collection_entry_id') == collection_entry_id
                    and bool(status.get('inputs'))
                )
                if not inputs and not read_before:
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
    if status.get('phase') not in ('completed', 'failed') and not _job_running(status):
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
    confirmed = await _confirmations(client, voice, upload_id)
    rejected = await _rejections(client, voice, upload_id)
    # what Undo last takes back
    status['last_decision'] = _last_decision(confirmed, rejected)
    return _with_confirmations(status, confirmed, rejected)


async def _confirmations(client, voice: VoiceElnService, upload_id: str) -> dict | None:
    """The confirmations file as loaded (old records left out), as JSON;
    None when it is damaged: the confirm endpoint says so."""
    raw = await voice.writer.read_raw_file(client, upload_id, CONFIRMATIONS_FILE)
    try:
        return confirmations_json(load_confirmations(read_confirmations(raw)))
    except ValueError:
        return None


async def _rejections(client, voice: VoiceElnService, upload_id: str) -> dict | None:
    """The rejections file; None when it is damaged (the reject endpoint
    refuses on it): it then shows as no rejections, and as none changed
    since the last run."""
    raw = await voice.writer.read_raw_file(client, upload_id, REJECTIONS_FILE)
    try:
        return read_rejections(raw)
    except ValueError:
        return None


async def _loaded_rejections(client, voice: VoiceElnService, upload_id: str) -> dict:
    raw = await voice.writer.read_raw_file(client, upload_id, REJECTIONS_FILE)
    try:
        return read_rejections(raw)
    except ValueError as why:
        raise HTTPException(
            status_code=409, detail=f'rejections file damaged: {why}'
        ) from why


async def _loaded_confirmations(client, voice: VoiceElnService, upload_id: str) -> dict:
    raw = await voice.writer.read_raw_file(client, upload_id, CONFIRMATIONS_FILE)
    try:
        return load_confirmations(read_confirmations(raw))
    except ValueError as why:
        # a damaged audit file is never overwritten; this needs a look
        raise HTTPException(
            status_code=409, detail=f'confirmations file damaged: {why}'
        ) from why


async def _applied_inputs(
    client, voice: VoiceElnService, upload_id: str, collection_entry_id: str
) -> set[str]:
    """The inputs (entry ids) named by a confirmation that is still one
    of their edits: an earlier input revised or deleted leaves it
    matching nothing once the job read it again. None without this
    collection's status: the upload's sheet is not its. A damaged file
    is read as none here (a listing); the confirm endpoint refuses on it."""
    raw = await voice.writer.read_raw_file(client, upload_id, CONFIRMATIONS_FILE)
    try:
        confirmed = load_confirmations(read_confirmations(raw))
    except ValueError:
        return set()
    status = await voice.writer.read_json(client, upload_id, STATUS_FILE)
    if status is None or status.get('collection_entry_id') != collection_entry_id:
        return set()
    # an input a running job has not reported yet counts by its record
    # a held key is not in the sheet (finding T)
    ready = {
        report.get('entry_id'): {edit['key'] for edit in report.get('ready', [])}
        - set(report.get('held', []))
        for report in status.get('inputs', [])
        if isinstance(report, dict)
    }
    return {
        c.input
        for key, c in confirmed.items()
        if c.input not in ready or key in ready[c.input]
    }


async def _input_of(
    client,
    voice: VoiceElnService,
    upload_id: str,
    collection_entry_id: str,
    entry_id: str,
):
    """The collection's input of this entry id; None when it is not one
    (the revise or delete that follows refuses it)."""
    inputs = await voice.collect_inputs(
        client, upload_id, collection_entry_id=collection_entry_id
    )
    return next((i for i in inputs if i.entry_id == entry_id), None)


async def _drop_decisions(
    client, voice: VoiceElnService, upload_id: str, entry_id: str
) -> None:
    """Remove the confirmations and rejections of an input that was
    revised or deleted: they were bound to its old words, and words
    changed back must not count as decided again. The next job reads it
    (and every later input, whose sheet before changed) anew. Both files
    are read before either is written: a damaged one changes nothing."""
    confirmed = await _loaded_confirmations(client, voice, upload_id)
    rejected = await _loaded_rejections(client, voice, upload_id)
    kept = {key: c for key, c in confirmed.items() if c.input != entry_id}
    if len(kept) < len(confirmed):
        await voice.writer.write_json(
            client, upload_id, CONFIRMATIONS_FILE, confirmations_json(kept)
        )
    still = {key: r for key, r in rejected.items() if r['input'] != entry_id}
    if len(still) < len(rejected):
        await voice.writer.write_json(client, upload_id, REJECTIONS_FILE, still)


def _with_confirmations(
    status: dict, confirmed: dict | None, rejected: dict | None = None
) -> dict:
    """The reports say what the job knew when it ran; a confirmation made
    since shows at once, as `confirmed_since`, one removed since (its
    input revised or deleted) as `unconfirmed_since`: both wait for the
    next run. `held`: confirmed, not written until the inputs above are
    decided (apply.py). `rejected`: the ready edits rejected, never
    written; `rejected_since`, `unrejected_since` as for confirmations.
    `decide_again`: how many decisions of the input match none of its
    edits, because an update above changed what it shows. A damaged
    confirmations file (None) changes nothing the job reported."""
    for report in status.get('inputs', []):
        if not isinstance(report, dict):
            continue
        written = report.get('confirmed', [])
        keys = [edit['key'] for edit in report.get('ready', [])]
        entry_id = report.get('entry_id')
        if confirmed is None or not entry_id:
            # a report from before reports named their input is read again
            report.update(
                confirmed_since=[],
                unconfirmed_since=[],
                held=[],
                rejected_since=[],
                unrejected_since=[],
                decide_again=0,
                rejected=_rejected(report, keys, rejected) if entry_id else [],
            )
            continue
        # two inputs of the same words on the same sheet share a key: a
        # confirmation is its own input's
        mine = {
            key
            for key, c in confirmed.items()
            if isinstance(c, dict) and c.get('input') == entry_id
        }
        # held: confirmed, waiting for the inputs above; no job writes it
        # before they are decided, so it is not "since"
        held = [key for key in report.get('held', []) if key in mine]
        since = [key for key in keys if key in mine and key not in written + held]
        report['unconfirmed_since'] = [key for key in written if key not in mine]
        report['confirmed'] = [*(k for k in written if k in mine), *held, *since]
        report['held'] = held
        report['confirmed_since'] = since
        # unlike a confirmation, a rejection holds for every input with
        # its key: nothing is written, so two inputs of the same words
        # keep sharing it, and the second would wait for a decision
        # the server refuses
        # a rejection made or taken back since the run changes the preview
        # the inputs after it were read on: they wait for the next run. A
        # report from before reports carried it knew none
        known = report.get('rejected')
        if not isinstance(known, list):
            known = []
        # a key the run knew as rejected and confirmed since: the inputs
        # after it were read without the edit
        flipped = [key for key in known if key in since]
        # a key this input confirmed is not its rejection, whoever holds one
        known = [key for key in known if key not in mine]
        now = (
            # a damaged rejections file is unknown, not undone: what the
            # run knew stands
            known
            if rejected is None
            else [key for key in keys if key not in mine and key in rejected]
        )
        report['rejected_since'] = [key for key in now if key not in known]
        report['unrejected_since'] = [
            *(key for key in known if key not in now),
            *flipped,
        ]
        report['rejected'] = now
        # only with something to do: edits to decide, or a part to
        # rephrase; an edit the sheet already holds needs nothing
        decided = mine | set(report['rejected'])
        todo = any(key not in decided for key in keys) or any(
            report.get(field) for field in ('unresolved', 'not_applied', 'refused')
        )
        lost = mine - set(keys)
        if rejected is not None:
            lost |= {
                key
                for key, r in rejected.items()
                if isinstance(r, dict) and r.get('input') == entry_id
            } - set(keys)
        report['decide_again'] = todo * len(lost)
    return status


def _rejected(report: dict, keys: list[str], rejected: dict | None) -> list[str]:
    """The report's rejected keys; the run's own list while the file is
    damaged."""
    if rejected is None:
        known = report.get('rejected')
        return [k for k in known if k in keys] if isinstance(known, list) else []
    return [k for k in keys if k in rejected]


def _undecided(report: dict) -> list[str]:
    decided = {*report.get('confirmed', []), *report.get('rejected', [])}
    return [e['key'] for e in report.get('ready', []) if e['key'] not in decided]


def _outdated(status: dict, texts: dict[str, str]) -> set[str]:
    """The inputs (entry ids) whose report is not what the next run will
    read: one whose words changed or that is gone, and every one after it
    or after a decision above the last run did not know that changes the
    sheet they are read on (`_since_last_run`). Of a status with the
    overlay; `texts`: the inputs' words now, by entry id."""
    outdated: set[str] = set()
    stale = False
    for report in status.get('inputs', []):
        if not isinstance(report, dict):
            continue
        entry_id = report.get('entry_id')
        if texts.get(entry_id) != report.get('text'):
            stale = True
        if stale:
            outdated.add(entry_id)
        if _since_last_run(report):
            stale = True
    return outdated


def _since_last_run(report: dict) -> bool:
    """A decision the last run did not know that changes the preview the
    inputs after this one were read on. Not a confirmation: the preview
    already counted the edit."""
    return any(
        report.get(field)
        for field in ('unconfirmed_since', 'rejected_since', 'unrejected_since')
    )


def _withdrawn(status: dict) -> bool:
    """Whether a confirmation the stored sheet was written with is gone,
    or may be: a rejection taken back changes the sheet the inputs after
    it were confirmed on (finding R)."""
    return any(
        report.get('unconfirmed_since') or report.get('unrejected_since')
        for report in status.get('inputs', [])
        if isinstance(report, dict)
    )


def _not_settled(status: dict, texts: dict[str, str]) -> str | None:
    """Why the reports are not what the next run reads, or None: every
    input read in its words, nothing decided or changed since the last
    run. Of a status with the overlay."""
    reports = [r for r in status.get('inputs', []) if isinstance(r, dict)]
    if any(_since_last_run(r) or r.get('confirmed_since') for r in reports):
        return (
            'decisions were made since the last run; the sheet is being written again'
        )
    read = {r.get('entry_id'): r.get('text') for r in reports}
    if read.keys() - texts.keys():
        return (
            'an update was deleted since the last run; the sheet is being written again'
        )
    # a recording being transcribed has no words yet, and no report
    if any(
        text is None or entry_id not in read or read[entry_id] != text
        for entry_id, text in texts.items()
    ):
        return 'an update is not read yet; wait for it'
    return None


def _not_final(status: dict, texts: dict[str, str]) -> str | None:
    """Why the stored sheet is not the one to download, or None: settled,
    and every edit decided."""
    why = _not_settled(status, texts)
    if why:
        return why
    reports = [r for r in status.get('inputs', []) if isinstance(r, dict)]
    undecided = sum(len(_undecided(r)) for r in reports)
    if undecided:
        return f'decide every edit first: {undecided} waiting for a decision'
    return None


_WITHDRAWN = (
    'an update written into the sheet was revised or deleted, or a '
    'rejection taken back; the sheet is being written again'
)


async def _completed_status(
    client, voice: VoiceElnService, upload_id: str, collection_entry_id: str
) -> dict:
    """The status of the last job, which must have completed: what the
    final sheet and the changelog are checked against."""
    status = await _edit_status(client, voice, upload_id, collection_entry_id)
    if _job_running(status):
        raise HTTPException(
            status_code=409, detail='an edit job is running; wait for it'
        )
    if status.get('phase') != 'completed':
        raise HTTPException(
            status_code=409, detail='the last edit job did not complete; run it again'
        )
    if _withdrawn(status):
        raise HTTPException(status_code=409, detail=_WITHDRAWN)
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


async def _decidable(  # noqa: PLR0913
    client,
    voice: VoiceElnService,
    upload_id: str,
    collection_entry_id: str,
    *,
    keys: list[str],
    verb: str,
    review: bool,
) -> tuple[dict, dict]:
    """The last job's status and, by key, the report each key is a ready
    edit of: a key to confirm or reject must be one, of an input whose
    words are the ones the report read. One by one, the keys must be the
    next input's to decide (`_turn`); from the Confirm all review
    (`review`), of any input, once every input is read."""
    status = await _edit_status(client, voice, upload_id, collection_entry_id)
    if review and _job_running(status):
        raise HTTPException(
            status_code=409,
            detail=f'the updates are being read; {verb} when it has finished',
        )
    # a key two inputs share is the first one's: the job writes it
    # there, and the later input's key changes with the sheet
    reports = {}
    for report in status.get('inputs', []):
        for edit in report.get('ready', []):
            reports.setdefault(edit['key'], report)
    unknown = sorted(set(keys) - set(reports))
    if unknown and _job_running(status):
        # the running job's reports replace the old ones as it goes
        raise HTTPException(
            status_code=409,
            detail=f'an edit job is running; {verb} when it has finished',
        )
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f'not a ready edit of the last job: {", ".join(unknown)}',
        )
    if any(not reports[key].get('entry_id') for key in keys):
        # a report from before reports named their input
        raise HTTPException(
            status_code=409,
            detail='the last run does not name its updates; '
            f'read the updates again, then {verb}',
        )
    # a decision is on the words the report read, and on the sheet the
    # inputs above leave: not on a report the next run reads anew
    inputs = await voice.collect_inputs(
        client, upload_id, collection_entry_id=collection_entry_id
    )
    texts = {i.entry_id: i.text for i in inputs}
    if any(
        texts.get(reports[key].get('entry_id')) != reports[key].get('text')
        for key in keys
    ):
        raise HTTPException(
            status_code=409,
            detail='the words of an input changed since the last run; '
            f'apply the updates again, then {verb}',
        )
    outdated = _outdated(status, texts)
    if any(reports[key].get('entry_id') in outdated for key in keys):
        raise HTTPException(
            status_code=409,
            detail='an update above changed: this one is read again first; '
            f'{verb} it when it is ready',
        )
    if review:
        why = _not_settled(status, texts)
        if why:
            raise HTTPException(status_code=409, detail=why)
    elif any(reports[key] is not _turn(status) for key in keys):
        raise HTTPException(
            status_code=409,
            detail='decide the updates above first, or decide all at once '
            'with Confirm all',
        )
    return status, reports


def _turn(status: dict) -> dict | None:
    """The first report with an edit nobody decided: an edit is read on
    the sheet the ones above leave, so it is decided after them."""
    return next(
        (r for r in status.get('inputs', []) if isinstance(r, dict) and _undecided(r)),
        None,
    )


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
    job's reports. An already confirmed key keeps its first record. From
    the Confirm all review, the keys are every undecided edit."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    if not body.keys:
        raise HTTPException(status_code=400, detail='no keys to confirm')
    try:
        async with _upload_lock(upload_id), voice.build_client(token) as client:
            status, reports = await _decidable(
                client,
                voice,
                upload_id,
                collection_entry_id,
                keys=body.keys,
                verb='confirm',
                review=body.review,
            )
            if body.review and set(body.keys) != {
                key
                for report in status.get('inputs', [])
                if isinstance(report, dict)
                for key in _undecided(report)
            }:
                # what is confirmed is what the review showed, all of it
                raise HTTPException(
                    status_code=409,
                    detail='the edits changed since the review; look at them again',
                )
            confirmed = await _loaded_confirmations(client, voice, upload_id)
            rejected = await _loaded_rejections(client, voice, upload_id)
            refused = sorted(key for key in body.keys if key in rejected)
            if refused:
                raise HTTPException(
                    status_code=409,
                    detail=f'rejected, undo the rejection first: {", ".join(refused)}',
                )
            me = await _me(client)
            by = me.get('username') or me.get('name') or me['user_id']
            now = datetime.now(timezone.utc)
            for key in body.keys:
                confirmed.setdefault(
                    key,
                    Confirmation(by=by, at=now, input=reports[key].get('entry_id')),
                )
            # an input confirmed again: its confirmations that match none
            # of its edits (an earlier input changed) are done with
            ready = {
                reports[key].get('entry_id'): {
                    edit['key'] for edit in reports[key].get('ready', [])
                }
                for key in body.keys
            }
            confirmed = {
                key: c
                for key, c in confirmed.items()
                if c.input not in ready or key in ready[c.input]
            }
            await voice.writer.write_json(
                client, upload_id, CONFIRMATIONS_FILE, confirmations_json(confirmed)
            )
            await _drop_lost_rejections(client, voice, upload_id, rejected, ready)
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return ConfirmEditsResponse(confirmed=sorted(confirmed))


async def _drop_lost_rejections(
    client, voice: VoiceElnService, upload_id: str, rejected: dict, ready: dict
) -> None:
    """After a confirm: the rejections of the inputs decided that match
    none of their edits, as the confirmations are."""
    kept = {
        key: r
        for key, r in rejected.items()
        if r['input'] not in ready or key in ready[r['input']]
    }
    if len(kept) < len(rejected):
        await voice.writer.write_json(client, upload_id, REJECTIONS_FILE, kept)


@router.post(
    '/input-collections/{upload_id}/edits/reject', response_model=RejectEditsResponse
)
async def reject_edits(
    upload_id: str,
    body: RejectEditsRequest,
    request: Request,
    collection_entry_id: str,
) -> RejectEditsResponse:
    """Reject ready edits by their keys: never written, and decided. The
    record keeps the edit as proposed, with the optional reason, so sand
    can learn from it. A confirmed edit cannot be rejected; an already
    rejected key keeps its first record."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    if not body.keys:
        raise HTTPException(status_code=400, detail='no keys to reject')
    try:
        async with _upload_lock(upload_id), voice.build_client(token) as client:
            status, reports = await _decidable(
                client,
                voice,
                upload_id,
                collection_entry_id,
                keys=body.keys,
                verb='reject',
                review=body.review,
            )
            confirmed = await _loaded_confirmations(client, voice, upload_id)
            rejected = await _loaded_rejections(client, voice, upload_id)
            written = sorted(
                key
                for key in body.keys
                if key in confirmed
                and confirmed[key].input == reports[key].get('entry_id')
            )
            if written:
                raise HTTPException(
                    status_code=409,
                    detail=f'already confirmed: {", ".join(written)}',
                )
            me = await _me(client)
            by = me.get('username') or me.get('name') or me['user_id']
            # one click, one time: Undo last takes back what has the last
            at = datetime.now(timezone.utc).isoformat()
            for key in body.keys:
                rejected.setdefault(
                    key,
                    {
                        'by': by,
                        'at': at,
                        'input': reports[key].get('entry_id'),
                        'reason': body.reason,
                        'meant': body.meant.strip(),
                        'proposed': feedback.proposed_edit(status, key),
                    },
                )
            # as on confirm: an input decided again is done with its
            # rejections that match none of its edits
            ready = {
                reports[key].get('entry_id'): {
                    edit['key'] for edit in reports[key].get('ready', [])
                }
                for key in body.keys
            }
            rejected = {
                key: r
                for key, r in rejected.items()
                if r['input'] not in ready or key in ready[r['input']]
            }
            await voice.writer.write_json(client, upload_id, REJECTIONS_FILE, rejected)
            kept = {
                key: c
                for key, c in confirmed.items()
                if c.input not in ready or key in ready[c.input]
            }
            if len(kept) < len(confirmed):
                # a lost confirmation would count again if the earlier
                # input's words were changed back
                await voice.writer.write_json(
                    client, upload_id, CONFIRMATIONS_FILE, confirmations_json(kept)
                )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return RejectEditsResponse(rejected=sorted(rejected))


def _last_decision(confirmed: dict | None, rejected: dict | None) -> list[dict]:
    """The records of the last click, of either file: they share its time.
    JSON as loaded (`_confirmations`, `_rejections`)."""
    records = [
        {'key': key, 'decision': decision, 'at': datetime.fromisoformat(r['at'])}
        for decision, file in (('confirmed', confirmed), ('rejected', rejected))
        for key, r in (file or {}).items()
    ]
    if not records:
        return []
    last = max(r['at'] for r in records)
    return [
        {'key': r['key'], 'decision': r['decision']} for r in records if r['at'] == last
    ]


async def _idle(client, voice: VoiceElnService, upload_id: str) -> None:
    """Decisions are taken back between runs: a running job reads them."""
    if _job_running(await voice.writer.read_json(client, upload_id, STATUS_FILE)):
        raise HTTPException(
            status_code=409, detail='the updates are being read; wait for it'
        )


@router.post(
    '/input-collections/{upload_id}/edits/undo-last',
    response_model=UndoneResponse,
)
async def undo_last_decision(
    upload_id: str,
    body: DecisionsRequest,
    request: Request,
    collection_entry_id: str,
) -> UndoneResponse:
    """Take back the last click's decisions: its edits wait for a decision
    again. `keys`: the status's `last_decision`, so a click made since in
    another tab is not the one taken back."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    try:
        async with _upload_lock(upload_id), voice.build_client(token) as client:
            await _idle(client, voice, upload_id)
            confirmed = await _loaded_confirmations(client, voice, upload_id)
            rejected = await _loaded_rejections(client, voice, upload_id)
            last = _last_decision(confirmations_json(confirmed), rejected)
            if not last or {r['key'] for r in last} != set(body.keys):
                raise HTTPException(
                    status_code=409,
                    detail='this is not the last decision; look again',
                )
            undo = {(r['decision'], r['key']) for r in last}
            kept = {k: c for k, c in confirmed.items() if ('confirmed', k) not in undo}
            if len(kept) < len(confirmed):
                await voice.writer.write_json(
                    client, upload_id, CONFIRMATIONS_FILE, confirmations_json(kept)
                )
            still = {k: r for k, r in rejected.items() if ('rejected', k) not in undo}
            if len(still) < len(rejected):
                await voice.writer.write_json(client, upload_id, REJECTIONS_FILE, still)
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return UndoneResponse(undone=sorted(r['key'] for r in last))


@router.post('/input-collections/{upload_id}/edits/reset', status_code=204)
async def reset_decisions(
    upload_id: str,
    request: Request,
    collection_entry_id: str,
) -> None:
    """Take back every confirmation and rejection, as if none was made."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    try:
        async with _upload_lock(upload_id), voice.build_client(token) as client:
            await _idle(client, voice, upload_id)
            # damaged files are refused, not wiped: they need a look. Both
            # are read before either is written: one damaged changes nothing
            confirmed = await _loaded_confirmations(client, voice, upload_id)
            rejected = await _loaded_rejections(client, voice, upload_id)
            if confirmed:
                await voice.writer.write_json(client, upload_id, CONFIRMATIONS_FILE, {})
            if rejected:
                await voice.writer.write_json(client, upload_id, REJECTIONS_FILE, {})
    except NomadAPIError as exc:
        raise _http_error(exc) from exc


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
    """The sheet as an xlsx download under the sheet's name: the one the
    last completed job left (the original plus the confirmed edits), once
    every edit is decided; or the uploaded one while no job has written a
    sheet."""
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
            if status is not None and _job_running(status):
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
                confirmed = await _confirmations(client, voice, upload_id)
                rejected = await _rejections(client, voice, upload_id)
                inputs = await voice.collect_inputs(
                    client, upload_id, collection_entry_id=collection_entry_id
                )
                why = _not_final(
                    _with_confirmations(status, confirmed, rejected),
                    {i.entry_id: i.text for i in inputs},
                )
                if why is not None:
                    raise HTTPException(status_code=409, detail=why)
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
    snapshot of what the user saw. With `edit`, the report is about one
    proposed edit; nothing is written to or withheld from the sheet."""
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
            edit = None
            if body.edit is not None:
                edit = {
                    'key': body.edit.key,
                    'reason': body.edit.reason,
                    'confirmed': body.edit.key in (confirmed or {}),
                    'proposed': feedback.proposed_edit(status, body.edit.key),
                }
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
                edit=edit,
            )
            file = feedback.report_file(at)
            await voice.writer.write_json(client, upload_id, file, report)
    except NomadAPIError as exc:
        raise _http_error(exc) from exc
    return FeedbackResponse(file=file)
