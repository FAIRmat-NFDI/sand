from typing import Any

from pydantic import BaseModel


class ExtractRequest(BaseModel):
    text: str


class ExtractResponse(BaseModel):
    processes: list[dict[str, Any]]
