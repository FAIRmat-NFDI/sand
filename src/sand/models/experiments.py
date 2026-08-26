from pydantic import BaseModel, Field


class HysprintExperimentInfoForm(BaseModel):
    project_name: str = Field(min_length=1)
    batch: str = Field(min_length=1)
    subbatch: str = Field(min_length=1)
    first_sample: str = Field(min_length=1)
    n_samples: int = Field(ge=1)


class CreateHysprintExperimentRequest(BaseModel):
    name: str | None = None
    info: HysprintExperimentInfoForm | None = None


class InputCollectionResponse(BaseModel):
    upload_id: str
    entry_id: str
    entry_url: str


class InputCollectionSummaryModel(InputCollectionResponse):
    name: str


class InputCollectionListResponse(BaseModel):
    input_collections: list[InputCollectionSummaryModel]


class CreateNoteRequest(BaseModel):
    text: str
