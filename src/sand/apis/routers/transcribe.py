from fastapi import APIRouter, HTTPException, Request, UploadFile

from sand.apis.deps import get_bearer_token
from sand.models.transcribe import TranscribeResponse
from sand.services.nomad_upload import NomadAPIError, NomadAuthError
from sand.services.voice_eln import (
    TranscriptionFailedError,
    TranscriptionTimeoutError,
    VoiceElnService,
)

router = APIRouter()

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


@router.post('/transcribe', response_model=TranscribeResponse)
async def transcribe(
    file: UploadFile,
    request: Request,
) -> TranscribeResponse:
    """Create an AudioInput entry in NOMAD and return its machine transcript.

    The audio is not transcribed here: it is uploaded to NOMAD, where the
    voice-eln plugin creates an AudioInput entry and transcribes it; this
    endpoint waits for the transcript and returns it together with the entry.
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
            result = await voice.transcribe_via_entry(client, audio, filename)
    except NomadAuthError as exc:
        raise HTTPException(status_code=401, detail=exc.detail) from exc
    except TranscriptionFailedError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except TranscriptionTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except NomadAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return TranscribeResponse(
        text=result.text,
        audio_upload_id=result.upload_id,
        audio_entry_url=result.entry_url,
    )
