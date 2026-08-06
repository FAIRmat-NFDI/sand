from pydantic import BaseModel


class TranscribeResponse(BaseModel):
    text: str
    # The AudioInput entry in NOMAD the transcript was read from.
    audio_upload_id: str | None = None
    audio_entry_url: str | None = None
