from pydantic import BaseModel


class TranscribeResponse(BaseModel):
    text: str
