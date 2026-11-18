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
    # the edits decided without a confirmation (rejections.json's keys);
    # a job started before rejections existed has none
    rejections: list[str] = []
    # one per text: a decision is its input's (apply.py); none in a job
    # started before they were passed
    input_ids: list[str] = []


class StoreInput(JobInput):
    """The job, and its status without the store's results: written as
    completed with the final sheet, in one request. Empty in a job
    started before (its workflow writes the status itself)."""

    status: dict = {}


class ReplayInput(JobInput):
    entry_ids: list[str]  # one per text, for the reports
    # with every input cached, the replay stores the final sheet itself,
    # with this status as completed (StoreInput); empty: it never does
    store_status: dict = {}


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
