from pydantic import BaseModel


class ExperimentInfoForm(BaseModel):
    """The experiment-info form (docs/handover.md §8, 'option A').

    Stored as JSON in a WrittenNote labeled 'experiment_info'; never parsed
    out of prose because the ids are high-stakes (they generate the lab_ids).
    """

    project_name: str
    batch: str
    subbatch: str
    first_sample: str
    n_samples: int
    date: str | None = None


class CreateExperimentRequest(BaseModel):
    name: str | None = None
    info: ExperimentInfoForm | None = None


class InputCollectionResponse(BaseModel):
    """An entry created in an experiment (collection, audio, or note)."""

    upload_id: str
    entry_id: str
    entry_url: str


class ExperimentSummaryModel(BaseModel):
    upload_id: str
    entry_id: str
    name: str
    entry_url: str


class InputCollectionListResponse(BaseModel):
    input_collections: list[ExperimentSummaryModel]


class CreateNoteRequest(BaseModel):
    text: str
