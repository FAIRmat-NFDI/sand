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
from sand.services.voice_eln import EXPERIMENT_INFO_LABEL, VoiceElnService

# Hysprint-specific: where the experiment-info form note is stored.
EXPERIMENT_INFO_MAINFILE = 'experiment_info.archive.json'

router = APIRouter()

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _voice_service(request: Request) -> VoiceElnService:
    return request.app.state.voice_eln


def _http_error(exc: NomadAPIError) -> HTTPException:
    if isinstance(exc, NomadAuthError):
        return HTTPException(status_code=401, detail=exc.detail)
    return HTTPException(status_code=502, detail=str(exc))


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
                upload_id=e.upload_id,
                entry_id=e.entry_id,
                name=e.name,
                entry_url=voice.entry_url(e.upload_id, e.entry_id),
            )
            for e in input_collections
        ]
    )


@router.post('/input-collections', response_model=InputCollectionResponse)
async def create_hysprint_input_collection(
    body: CreateHysprintExperimentRequest,
    request: Request,
) -> InputCollectionResponse:
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
            result = await voice.create_input_collection(client, name)
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

    return InputCollectionResponse(
        upload_id=result.upload_id,
        entry_id=result.entry_id,
        entry_url=voice.entry_url(result.upload_id, result.entry_id),
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

    filename = file.filename or 'audio.m4a'

    try:
        async with voice.build_client(token) as client:
            result = await voice.add_audio(client, upload_id, audio, filename)
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return InputCollectionResponse(
        upload_id=result.upload_id,
        entry_id=result.entry_id,
        entry_url=voice.entry_url(result.upload_id, result.entry_id),
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
        upload_id=result.upload_id,
        entry_id=result.entry_id,
        entry_url=voice.entry_url(result.upload_id, result.entry_id),
    )
