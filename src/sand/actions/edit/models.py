from pydantic import BaseModel


class EditInput(BaseModel):
    upload_id: str
    user_id: str
    collection_entry_id: str


class JobInput(BaseModel):
    """What every activity after collecting needs: the job's identity and
    the inputs as collected, so a text edited while the job runs is not
    seen halfway."""

    upload_id: str
    user_id: str
    job_id: str
    llm_model_name: str
    texts: list[str]


class PrepareInput(JobInput):
    number: int  # 1-based position in `texts`


class Answer(BaseModel):
    """One pass's answer: the model's JSON as text, or why there is none."""

    text: str | None = None
    error: str | None = None


class FinishInput(PrepareInput):
    entry_id: str
    answers: list[Answer]  # empty when the proposal came from the cache


class WriteStatusInput(BaseModel):
    upload_id: str
    user_id: str
    status: dict
