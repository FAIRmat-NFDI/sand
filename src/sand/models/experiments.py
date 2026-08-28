from pydantic import BaseModel, Field


class HysprintExperimentInfoForm(BaseModel):
    project_name: str = Field(min_length=1)
    batch: str = Field(min_length=1)
    subbatch: str = Field(min_length=1)
    first_sample: str = Field(min_length=1)
    n_samples: int = Field(ge=1)

    def default_name(self) -> str:
        """The lab naming convention experiments follow; lab_ids derive
        from these parts (docs/handover.md), so it lives on the model
        that owns the fields."""
        return f'{self.project_name}_{self.batch}_{self.subbatch}'


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


class HysprintExtractResponse(BaseModel):
    """The extracted hysprint archive; step_types = the chosen type per
    step in plan order (a quick sanity check of the SELECT stage)."""

    archive: dict
    step_types: list[str]
