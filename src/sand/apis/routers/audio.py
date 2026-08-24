from fastapi import APIRouter, HTTPException, Request, UploadFile

from sand.apis.deps import get_bearer_token
from sand.models.audio import AudioEntryResponse
from sand.services.nomad_upload import NomadAPIError, NomadAuthError
from sand.services.voice_eln import VoiceElnService

router = APIRouter()

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


@router.post('/audio', response_model=AudioEntryResponse)
async def create_audio_entry(
    file: UploadFile,
    request: Request,
) -> AudioEntryResponse:
    """Upload the audio to NOMAD and return the resulting AudioInput entry.

    The audio is not transcribed here: the voice-eln plugin creates an
    AudioInput entry from the uploaded file and transcribes it inside NOMAD.
    This endpoint only returns the link to that entry.
    """
    voice: VoiceElnService = request.app.state.voice_eln
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
            result = await voice.create_audio_entry(client, audio, filename)
    except NomadAuthError as exc:
        raise HTTPException(status_code=401, detail=exc.detail) from exc
    except NomadAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return AudioEntryResponse(
        upload_id=result.upload_id,
        entry_id=result.entry_id,
        entry_url=result.entry_url,
    )
