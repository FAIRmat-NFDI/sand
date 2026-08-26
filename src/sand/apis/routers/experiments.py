import json

from fastapi import APIRouter, HTTPException, Request, UploadFile

from sand.apis.deps import get_bearer_token
from sand.models.experiments import (
    CreateHysprintExperimentRequest,
    CreateNoteRequest,
    InputCollectionListResponse,
    InputCollectionResponse,
    InputCollectionSummaryModel,
)
from sand.services.nomad_upload import NomadAPIError, NomadAuthError
from sand.services.voice_eln import (
    AUDIO_EXTENSIONS,
    EXPERIMENT_INFO_LABEL,
    EXPERIMENT_INFO_MAINFILE,
    EXPERIMENT_MAINFILE,
    VoiceElnService,
    normalize_audio_filename,
)

router = APIRouter()

# Keep in sync with MAX_SIZE in apis/static/index.html.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# NomadAPIError status codes that are the caller's fault, not NOMAD's:
# pass them through instead of reporting a gateway failure.
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
    name = body.name
    if not name and info:
        name = f'{info["project_name"]}_{info["batch"]}_{info["subbatch"]}'
    if not name:
        raise HTTPException(
            status_code=400, detail='Provide a name or the experiment info'
        )

    try:
        async with voice.build_client(token) as client:
            result = await voice.create_input_collection(
                client, name, EXPERIMENT_MAINFILE
            )
            if info:
                await voice.add_written_note(
                    client,
                    result.upload_id,
                    text=json.dumps(info),
                    label=EXPERIMENT_INFO_LABEL,
                    note_mainfile=EXPERIMENT_INFO_MAINFILE,
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
) -> InputCollectionResponse:
    """Add audio to inputCollection entry."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    filename = normalize_audio_filename(file.filename or 'audio.m4a')
    if filename is None:
        raise HTTPException(
            status_code=415,
            detail='Unsupported audio format; use one of: '
            + ', '.join(sorted(AUDIO_EXTENSIONS)),
        )

    buf = bytearray()
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail='File too large (max 25 MB)')

    audio = bytes(buf)
    if not audio:
        raise HTTPException(status_code=400, detail='Uploaded file is empty')

    try:
        async with voice.build_client(token) as client:
            result = await voice.add_audio(client, upload_id, audio, filename)
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
) -> InputCollectionResponse:
    """Add a typed step note (WrittenNote labeled 'step') to the experiment."""
    voice = _voice_service(request)
    token = get_bearer_token(request)

    if not body.text.strip():
        raise HTTPException(status_code=400, detail='Note text is empty')

    try:
        async with voice.build_client(token) as client:
            result = await voice.add_written_note(client, upload_id, body.text)
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return InputCollectionResponse(
        **_entry_response(voice, result.upload_id, result.entry_id)
    )
