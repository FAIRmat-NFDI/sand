from fastapi import APIRouter, HTTPException, Request

from sand.models.pipeline import CellResult, PipelineRequest, PipelineResponse
from sand.services.extraction import ExtractionService
from sand.services.hysprint_export import build_archive, process_display_name
from sand.services.nomad_upload import NomadUploader

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
    """Full pipeline: text -> extract process data -> build archive -> upload to NOMAD."""
    extraction: ExtractionService = request.app.state.extraction
    uploader: NomadUploader = request.app.state.nomad
    token = _get_bearer_token(request)

    if not body.text.strip():
        raise HTTPException(status_code=400, detail='Text is empty')

    try:
        data = await extraction.extract(body.text)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f'Extraction failed: {exc}'
        ) from exc

    archive = build_archive(data)
    if len(archive['data']) <= 1:  # only m_def left after pruning
        raise HTTPException(
            status_code=502,
            detail='No process data could be extracted from the text',
        )
    name = process_display_name(archive['data'])

    async with uploader.build_client(token) as client:
        try:
            upload = await uploader.upload_with_client(client, archive)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"NOMAD upload failed for '{name}': {exc}",
            ) from exc

    result = CellResult(
        name=name,
        upload_id=upload.upload_id,
        entry_url=upload.entry_url,
        archive=archive,
    )
    return PipelineResponse(cells=[result])
