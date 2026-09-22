import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request, Response, UploadFile

from sand.apis.deps import get_bearer_token
from sand.hysprint.sheet import (
    DERIVED_SHEET_MAINFILE,
    EXTRACTED_JSON_MAINFILE,
    EXTRACTION_STATUS_MAINFILE,
)
from sand.models.input_collections import (
    CreateHysprintExperimentRequest,
    CreateNoteRequest,
    ExtractJobResponse,
    InputCollectionListResponse,
    InputCollectionResponse,
    InputCollectionSummaryModel,
    InputItemModel,
    InputListResponse,
    ReviseInputRequest,
    ReviseInputResponse,
    SheetUploadResponse,
)
from sand.services.nomad_api import NomadAPIError, NomadAuthError, check_response
from sand.services.voice_eln import (
    AUDIO_EXTENSIONS,
    EXPERIMENT_INFO_LABEL,
    AudioUpload,
    DerivedSheet,
    VoiceElnService,
    normalize_audio_filename,
)

router = APIRouter()

# Keep in sync with MAX_SIZE in apis/static/index.html.
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
STALE_EXTRACTION_S = 30 * 60

# Serializes check-then-start per upload within this process, and the
# 'starting' reservation snapshot covers the window until the workflow's
# first own snapshot. A multi-process deployment could still race in the
# instant between two processes' checks - accepted for the lab scale.
_start_locks: dict[str, asyncio.Lock] = {}


def _start_lock(upload_id: str) -> asyncio.Lock:
    lock = _start_locks.get(upload_id)
    if lock is None:
        lock = _start_locks[upload_id] = asyncio.Lock()
    return lock


def _extraction_running(status: dict | None, collection_entry_id: str) -> bool:
    if not status or status.get('collection_entry_id') != collection_entry_id:
        return False
    if status.get('phase') in ('completed', 'failed'):
        return False
    updated = status.get('updated_at')
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(updated)
    except (TypeError, ValueError):
        return False
    return age.total_seconds() < STALE_EXTRACTION_S


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
                name=e.name, **_entry_response(voice, e.upload_id, e.entry_id)
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
                await voice.add_experiment_info(
                    client,
                    result.upload_id,
                    json.dumps(info),
                    collection_entry_id=result.entry_id,
                )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return InputCollectionSummaryModel(
        name=name, **_entry_response(voice, result.upload_id, result.entry_id)
    )


@router.post(
    '/input-collections/{upload_id}/audio', response_model=InputCollectionResponse
)
async def add_audio(
    upload_id: str,
    file: UploadFile,
    request: Request,
    collection_entry_id: str,
    transcript: str | None = Form(None),
) -> InputCollectionResponse:
    """Add audio to an InputCollection entry.

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
    """Add a typed step note (WrittenNote labeled 'step') to the experiment.

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
    """The experiment's inputs in extraction order (the experiment_info
    form note is not listed - it is edited through the form)."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    try:
        async with voice.build_client(token) as client:
            inputs = await voice.collect_inputs(
                client, upload_id, collection_entry_id=collection_entry_id
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
            )
            for item in inputs
            if item.label != EXPERIMENT_INFO_LABEL
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
        async with voice.build_client(token) as client:
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


XLSX_MEDIA_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


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
            xlsx = await voice.read_derived_sheet(
                client,
                upload_id,
                DERIVED_SHEET_MAINFILE,
                collection_entry_id=collection_entry_id,
            )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    if xlsx is None:
        raise HTTPException(
            status_code=404,
            detail='No sheet in this experiment yet; extract first',
        )
    return Response(
        content=xlsx,
        media_type=XLSX_MEDIA_TYPE,
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

    sheet = DerivedSheet(
        xlsx=xlsx,
        xlsx_mainfile=DERIVED_SHEET_MAINFILE,
        extraction=None,
        extraction_mainfile=EXTRACTED_JSON_MAINFILE,
    )
    try:
        async with voice.build_client(token) as client:
            changed, handle = await voice.replace_derived_sheet(
                client,
                upload_id,
                sheet,
                collection_entry_id=collection_entry_id,
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
    response_model=ExtractJobResponse,
)
async def start_extract_async(
    upload_id: str,
    request: Request,
    collection_entry_id: str,
) -> ExtractJobResponse:
    """SKELETON (issue #19, PR 1): start the extraction action and return
    a job id; poll /extract-status for progress. The workflow only writes
    status markers for now - PR 2 moves the real pipeline into it. The
    Extract button still uses the synchronous /extract.
    """
    voice = _voice_service(request)
    token = get_bearer_token(request)

    async with _start_lock(upload_id):
        try:
            async with voice.build_client(token) as client:
                status = await voice.read_status_file(
                    client, upload_id, EXTRACTION_STATUS_MAINFILE
                )
                if _extraction_running(status, collection_entry_id):
                    raise HTTPException(
                        status_code=409,
                        detail='an extraction is already running for this '
                        'experiment; wait for it to finish',
                    )
                # validates the collection (and the token) before starting
                await voice.collect_inputs(
                    client, upload_id, collection_entry_id=collection_entry_id
                )
                me = await client.get('/users/me')
                check_response(me, step='whoami')
                user_id = me.json()['user_id']
                # reservation: visible to any check until the workflow's
                # first own snapshot replaces it
                await voice.write_status_file(
                    client,
                    upload_id,
                    EXTRACTION_STATUS_MAINFILE,
                    {
                        'job_id': 'starting',
                        'phase': 'starting',
                        'collection_entry_id': collection_entry_id,
                        'updated_at': datetime.now(timezone.utc).isoformat(),
                        'steps': [],
                    },
                )
        except NomadAPIError as exc:
            raise _http_error(exc) from exc

        # the async variant: the sync one blocks the API event loop while
        # it sets up its Mongo/Temporal infrastructure
        from nomad.actions.manager import start_action_async

        from sand.actions.extract.models import ExtractInput

        try:
            job_id = await start_action_async(
                action_id='sand.actions.extract:extract_action_entry_point',
                data=ExtractInput(
                    upload_id=upload_id,
                    user_id=user_id,
                    collection_entry_id=collection_entry_id,
                ),
            )
        except Exception as exc:
            # release the reservation, or the guard would block retries
            # until the staleness timeout
            try:
                async with voice.build_client(token) as client:
                    await voice.write_status_file(
                        client,
                        upload_id,
                        EXTRACTION_STATUS_MAINFILE,
                        {
                            'job_id': 'starting',
                            'phase': 'failed',
                            'error': f'could not start the extraction: {exc}',
                            'collection_entry_id': collection_entry_id,
                            'updated_at': datetime.now(timezone.utc).isoformat(),
                            'steps': [],
                        },
                    )
            except NomadAPIError:
                pass
            raise HTTPException(
                status_code=502, detail=f'could not start the extraction: {exc}'
            ) from exc
    return ExtractJobResponse(job_id=job_id)


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
            status = await voice.read_status_file(
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
