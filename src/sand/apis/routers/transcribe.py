from fastapi import APIRouter, HTTPException, Request, UploadFile

from sand.models.transcribe import TranscribeResponse
from sand.services.stt import GroqSTTService

router = APIRouter()


MAX_UPLOAD_BYTES = 25 * 1024 * 1024


@router.post('/transcribe', response_model=TranscribeResponse)
async def transcribe(
    file: UploadFile,
    request: Request,
) -> TranscribeResponse:
    stt: GroqSTTService = request.app.state.stt

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

    filename = file.filename or 'audio'

    try:
        text = await stt.transcribe(audio, filename)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return TranscribeResponse(text=text)
