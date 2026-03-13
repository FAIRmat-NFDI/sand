from fastapi import APIRouter, HTTPException, Request

from sand_app.models.pipeline import PipelineRequest, PipelineResponse, ProcessResult
from sand_app.services.archive import build_archive
from sand_app.services.extraction import ExtractionService
from sand_app.services.nomad_upload import NomadUploader

router = APIRouter()


@router.post('/pipeline', response_model=PipelineResponse)
async def pipeline(
    body: PipelineRequest,
    request: Request,
) -> PipelineResponse:
    """Full pipeline: text -> extract processes -> build archives -> upload to NOMAD."""
    extraction: ExtractionService = request.app.state.extraction
    uploader: NomadUploader = request.app.state.nomad

    if not body.text.strip():
        raise HTTPException(status_code=400, detail='Text is empty')

    try:
        processes = await extraction.extract(body.text)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f'Extraction failed: {exc}'
        ) from exc

    results: list[ProcessResult] = []
    for process in processes:
        archive = build_archive(process)
        try:
            upload = await uploader.upload(archive)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"NOMAD upload failed for '{process.get('name', '?')}': {exc}",
            ) from exc

        results.append(
            ProcessResult(
                name=process.get('name', 'Unnamed'),
                upload_id=upload.upload_id,
                entry_url=upload.entry_url,
                archive=archive,
            )
        )

    return PipelineResponse(processes=results)
