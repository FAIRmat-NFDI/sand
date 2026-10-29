from pydantic import BaseModel, Field


class EditInput(BaseModel):
    upload_id: str
    user_id: str
    collection_entry_id: str


class JobInput(BaseModel):
    """What every activity after collecting needs: the job's identity,
    the inputs and the confirmations as collected. Both are taken once,
    so a text edited or an edit confirmed while the job runs is not seen
    halfway: the sheet every input is read against is the same one its
    answers are cached and written for. The next run sees them."""

    upload_id: str
    user_id: str
    job_id: str
    llm_model_name: str
    texts: list[str]
    confirmations: dict[str, dict]  # the file's JSON, by confirmation key


class PrepareInput(JobInput):
    number: int = Field(ge=1)  # 1-based position in `texts`


class LlmAnswer(BaseModel):
    """One pass's answer: the model's JSON as text, or why there is none."""

    text: str | None = None
    error: str | None = None


class ApplyInput(PrepareInput):
    entry_id: str
    answers: list[LlmAnswer]  # empty when the proposal came from the cache


class WriteStatusInput(BaseModel):
    upload_id: str
    user_id: str
    status: dict
