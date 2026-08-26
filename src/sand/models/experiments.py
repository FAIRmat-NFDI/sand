from pydantic import BaseModel


class HysprintExperimentInfoForm(BaseModel):
    project_name: str
    batch: str
    subbatch: str
    first_sample: str
    n_samples: int
    date: str | None = None


class CreateHysprintExperimentRequest(BaseModel):
    name: str | None = None
    info: HysprintExperimentInfoForm | None = None


class InputCollectionResponse(BaseModel):
    upload_id: str
    entry_id: str
    entry_url: str


class InputCollectionSummaryModel(BaseModel):
    upload_id: str
    entry_id: str
    name: str
    entry_url: str


class InputCollectionListResponse(BaseModel):
    input_collections: list[InputCollectionSummaryModel]


class CreateNoteRequest(BaseModel):
    text: str
