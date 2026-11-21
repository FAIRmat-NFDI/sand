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
    rejections: list[str]  # the edits decided without a confirmation
    input_ids: list[str]  # one per text: a decision is its input's (apply.py)


class StoreInput(JobInput):
    """The job, and its status without the store's results: written as
    completed with the final sheet, in one request."""

    status: dict


class ReplayInput(JobInput):
    entry_ids: list[str]  # one per text, for the reports
    # with every input cached, the replay stores the final sheet itself,
    # with this status as completed (StoreInput)
    store_status: dict
    # the inputs the job has reports of already: only the ones after
    # them are reported (their reports are in `store_status`)
    after: int = Field(default=0, ge=0)


class PrepareInput(JobInput):
    number: int = Field(ge=1)  # 1-based position in `texts`


class LlmAnswer(BaseModel):
    """One pass's answer: the model's JSON as text, or why there is none."""

    text: str | None = None
    error: str | None = None


class ApplyInput(PrepareInput):
    entry_id: str
    answers: list[LlmAnswer]  # empty when the proposal came from the cache
    # the job's status after this input, without its report: written with
    # the report and the job's own cache file in one request
    status: dict


class WriteStatusInput(BaseModel):
    upload_id: str
    user_id: str
    status: dict
