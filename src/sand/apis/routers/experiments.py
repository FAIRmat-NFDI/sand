import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile

from sand.apis.deps import voice_session
from sand.hysprint.generate import assemble, route_inputs
from sand.hysprint.sheet import (
    DERIVED_SHEET_MAINFILE,
    grid_to_xlsx_bytes,
    to_sheet,
)
from sand.hysprint.step_extractor import extract_step
from sand.models.experiments import (
    CreateHysprintExperimentRequest,
    CreateNoteRequest,
    HysprintExtractResponse,
    InputCollectionListResponse,
    InputCollectionResponse,
    InputCollectionSummaryModel,
)
from sand.services.extraction_service import ExtractionError, ExtractionService
from sand.services.voice_eln import (
    AUDIO_EXTENSIONS,
    VoiceElnSession,
    normalize_audio_filename,
)

router = APIRouter()

# Keep in sync with MAX_SIZE in apis/static/index.html.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# NomadAPIError and HysprintInputError raised below are translated to
# HTTP responses by the app-level exception handlers (see sand_api.py).
Session = Annotated[VoiceElnSession, Depends(voice_session)]


def _extraction_service(request: Request) -> ExtractionService:
    return request.app.state.extraction_service


def _entry_response(session: VoiceElnSession, upload_id: str, entry_id: str) -> dict:
    return {
        'upload_id': upload_id,
        'entry_id': entry_id,
        'entry_url': session.entry_url(upload_id, entry_id),
    }


@router.get('/input-collections', response_model=InputCollectionListResponse)
async def list_input_collections(session: Session) -> InputCollectionListResponse:
    """The user's unpublished experiments (InputCollection entries)."""
    input_collections = await session.list_input_collections()
    return InputCollectionListResponse(
        input_collections=[
            InputCollectionSummaryModel(
                name=e.name, **_entry_response(session, e.upload_id, e.entry_id)
            )
            for e in input_collections
        ]
    )


@router.post('/input-collections', response_model=InputCollectionSummaryModel)
async def create_hysprint_input_collection(
    body: CreateHysprintExperimentRequest,
    session: Session,
) -> InputCollectionSummaryModel:
    """Create an experiment: a NOMAD upload with an InputCollection entry.

    With `info`, the experiment-info form is stored alongside as a
    WrittenNote labeled 'experiment_info' and referenced by the collection.
    """
    info = body.info.model_dump(exclude_none=True) if body.info else None
    name = body.name or (body.info.default_name() if body.info else None)
    if not name:
        raise HTTPException(
            status_code=400, detail='Provide a name or the experiment info'
        )

    result = await session.create_input_collection(name)
    if info:
        await session.add_experiment_info(
            result.upload_id,
            json.dumps(info),
            collection_entry_id=result.entry_id,
        )

    return InputCollectionSummaryModel(
        name=name, **_entry_response(session, result.upload_id, result.entry_id)
    )


@router.post(
    '/input-collections/{upload_id}/audio', response_model=InputCollectionResponse
)
async def add_audio(
    upload_id: str,
    file: UploadFile,
    collection_entry_id: str,
    session: Session,
) -> InputCollectionResponse:
    """Add audio to an InputCollection entry.

    collection_entry_id names the target collection exactly (an upload
    can hold more than one).
    """
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

    result = await session.add_audio(
        upload_id, audio, filename, collection_entry_id=collection_entry_id
    )
    return InputCollectionResponse(
        **_entry_response(session, result.upload_id, result.entry_id)
    )


@router.post(
    '/input-collections/{upload_id}/notes', response_model=InputCollectionResponse
)
async def add_note(
    upload_id: str,
    body: CreateNoteRequest,
    collection_entry_id: str,
    session: Session,
) -> InputCollectionResponse:
    """Add a typed step note (WrittenNote labeled 'step') to the experiment.

    collection_entry_id names the target collection exactly (an upload
    can hold more than one).
    """
    if not body.text.strip():
        raise HTTPException(status_code=400, detail='Note text is empty')

    result = await session.add_written_note(
        upload_id, body.text, collection_entry_id=collection_entry_id
    )
    return InputCollectionResponse(
        **_entry_response(session, result.upload_id, result.entry_id)
    )


@router.post(
    '/input-collections/{upload_id}/extract', response_model=HysprintExtractResponse
)
async def extract_hysprint_experiment(
    upload_id: str,
    collection_entry_id: str,
    session: Session,
    extraction: Annotated[ExtractionService, Depends(_extraction_service)],
) -> HysprintExtractResponse:
    """Extract the experiment's inputs into the hysprint {samples, steps}
    archive, upload the resulting xlsx, and return the derived entry.
    """
    inputs = await session.collect_inputs(
        upload_id, collection_entry_id=collection_entry_id
    )

    pending = [i for i in inputs if i.text is None]
    if pending:
        raise HTTPException(
            status_code=409,
            detail=f'{len(pending)} input(s) not transcribed or processed yet; '
            'try again in a moment',
        )

    info, step_texts = route_inputs(inputs)

    async def extract_one(index: int, text: str) -> dict:
        step_name = f'step {index + 1} ({text[:60]!r})'
        try:
            return await extract_step(extraction, text)
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
    derived = await session.add_derived_sheet(
        upload_id,
        grid_to_xlsx_bytes(grid),
        DERIVED_SHEET_MAINFILE,
        collection_entry_id=collection_entry_id,
    )

    return HysprintExtractResponse(
        archive=archive,
        step_types=step_types,
        derived_entry=InputCollectionResponse(
            **_entry_response(session, derived.upload_id, derived.entry_id)
        ),
        sheet_issues=sheet_issues,
    )
