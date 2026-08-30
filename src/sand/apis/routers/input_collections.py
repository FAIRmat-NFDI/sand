import asyncio
import json

from fastapi import APIRouter, HTTPException, Request, UploadFile

from sand.apis.deps import get_bearer_token
from sand.hysprint.generate import HysprintInputError, assemble, route_inputs
from sand.hysprint.sheet import (
    DERIVED_SHEET_MAINFILE,
    grid_to_xlsx_bytes,
    to_sheet,
)
from sand.hysprint.step_extractor import extract_step
from sand.models.input_collections import (
    CreateHysprintExperimentRequest,
    CreateNoteRequest,
    HysprintExtractResponse,
    InputCollectionListResponse,
    InputCollectionResponse,
    InputCollectionSummaryModel,
)
from sand.services.extraction_service import ExtractionError, ExtractionService
from sand.services.nomad_api import NomadAPIError, NomadAuthError
from sand.services.voice_eln import (
    AUDIO_EXTENSIONS,
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
        async with voice.session(token) as session:
            input_collections = await session.list_input_collections()
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
        async with voice.session(token) as session:
            result = await session.create_input_collection(name)
            if info:
                await session.add_experiment_info(
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
) -> InputCollectionResponse:
    """Add audio to an InputCollection entry.

    collection_entry_id names the target collection exactly (an upload
    can hold more than one).
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
        async with voice.session(token) as session:
            result = await session.add_audio(
                upload_id,
                audio,
                filename,
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
        async with voice.session(token) as session:
            result = await session.add_written_note(
                upload_id,
                body.text,
                collection_entry_id=collection_entry_id,
            )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return InputCollectionResponse(
        **_entry_response(voice, result.upload_id, result.entry_id)
    )


@router.post(
    '/input-collections/{upload_id}/extract', response_model=HysprintExtractResponse
)
async def extract_hysprint_experiment(
    upload_id: str,
    request: Request,
    collection_entry_id: str,
) -> HysprintExtractResponse:
    """Extract the experiment's inputs into the hysprint {samples, steps}
    archive, upload the resulting xlsx, and return the derived entry.
    """
    voice = _voice_service(request)
    runner: ExtractionService = request.app.state.extraction_service
    token = get_bearer_token(request)

    try:
        async with voice.session(token) as session:
            inputs = await session.collect_inputs(
                upload_id, collection_entry_id=collection_entry_id
            )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    pending = [i for i in inputs if i.text is None]
    if pending:
        raise HTTPException(
            status_code=409,
            detail=f'{len(pending)} input(s) not transcribed or processed yet; '
            'try again in a moment',
        )

    try:
        info, step_texts = route_inputs(inputs)
    except HysprintInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def extract_one(index: int, text: str) -> dict:
        step_name = f'step {index + 1} ({text[:60]!r})'
        try:
            return await extract_step(runner, text)
        except ExtractionError as exc:
            raise HTTPException(
                status_code=502, detail=f'LLM extraction failed for {step_name}: {exc}'
            ) from exc
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504, detail=f'LLM extraction timed out for {step_name}'
            ) from exc

    slots = await asyncio.gather(
        *(extract_one(i, text) for i, text in enumerate(step_texts))
    )

    try:
        archive = assemble(info, list(slots))
    except ValueError as exc:
        # e.g. a narration names a sample label the form did not declare
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    step_types = [slot['step_type'] for slot in slots]
    grid, sheet_issues = to_sheet(archive)
    xlsx = grid_to_xlsx_bytes(grid)
    try:
        async with voice.session(token) as session:
            derived = await session.add_derived_sheet(
                upload_id,
                xlsx,
                DERIVED_SHEET_MAINFILE,
                collection_entry_id=collection_entry_id,
            )
    except NomadAPIError as exc:
        raise _http_error(exc) from exc

    return HysprintExtractResponse(
        archive=archive,
        step_types=step_types,
        derived_entry=InputCollectionResponse(
            **_entry_response(voice, derived.upload_id, derived.entry_id)
        ),
        sheet_issues=sheet_issues,
    )
