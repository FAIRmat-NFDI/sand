from typing import Any

from pydantic import BaseModel


class PipelineRequest(BaseModel):
    text: str


class CellResult(BaseModel):
    name: str
    upload_id: str
    entry_url: str
    archive: dict[str, Any]


class PipelineResponse(BaseModel):
    cells: list[CellResult]
