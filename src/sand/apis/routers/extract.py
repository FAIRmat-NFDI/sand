from fastapi import APIRouter, HTTPException, Request

from sand.models.extract import ExtractRequest, ExtractResponse
from sand.services.extraction import ExtractionService

router = APIRouter()


@router.post('/extract', response_model=ExtractResponse)
async def extract(
    body: ExtractRequest,
    request: Request,
) -> ExtractResponse:
    extraction: ExtractionService = request.app.state.extraction

    if not body.text.strip():
        raise HTTPException(status_code=400, detail='Text is empty')

    try:
        cells = await extraction.extract(body.text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ExtractResponse(cells=cells)
