from pydantic import BaseModel


class AudioEntryResponse(BaseModel):
    """The AudioInput entry in NOMAD created from the uploaded audio."""

    upload_id: str
    entry_id: str
    entry_url: str
