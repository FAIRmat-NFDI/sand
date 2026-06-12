from fastapi import APIRouter, HTTPException, Request

from sand_app.models.pipeline import CellResult, PipelineRequest, PipelineResponse
from sand_app.services.extraction import ExtractionService
from sand_app.services.nomad_upload import NomadUploader
from sand_app.services.perovskite_export import (
    cell_display_name,
    convert_cells_to_nomad_entries,
)

router = APIRouter()


def _get_bearer_token(request: Request) -> str:
    """Extract the Bearer token from the Authorization header."""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth.removeprefix('Bearer ')
    raise HTTPException(status_code=401, detail='Missing or invalid Authorization header')


@router.post('/pipeline', response_model=PipelineResponse)
async def pipeline(
    body: PipelineRequest,
    request: Request,
) -> PipelineResponse:
    """Full pipeline: text -> extract cells -> build archives -> upload to NOMAD."""
    extraction: ExtractionService = request.app.state.extraction
    uploader: NomadUploader = request.app.state.nomad
    token = _get_bearer_token(request)

    if not body.text.strip():
        raise HTTPException(status_code=400, detail='Text is empty')

    try:
        cells = await extraction.extract(body.text)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f'Extraction failed: {exc}'
        ) from exc

    # Postprocess, validate, filter, and split into one NOMAD entry per cell.
    try:
        entries = convert_cells_to_nomad_entries(cells, source_text=body.text)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f'Preparing NOMAD entries failed: {exc}'
        ) from exc

    results: list[CellResult] = []
    for archive in entries:
        name = cell_display_name(archive['data'])
        try:
            upload = await uploader.upload(archive, token=token)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"NOMAD upload failed for '{name}': {exc}",
            ) from exc

        results.append(
            CellResult(
                name=name,
                upload_id=upload.upload_id,
                entry_url=upload.entry_url,
                archive=archive,
            )
        )

    return PipelineResponse(cells=results)
