from pydantic import BaseModel


class ExtractInput(BaseModel):
    upload_id: str
    user_id: str
    collection_entry_id: str


class WriteStatusInput(BaseModel):
    upload_id: str
    user_id: str
    status: dict
