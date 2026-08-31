from pydantic import BaseModel, Field


class HysprintExperimentInfoForm(BaseModel):
    project_name: str = Field(min_length=1)
    batch: str = Field(min_length=1)
    subbatch: str = Field(min_length=1)
    first_sample: str = Field(min_length=1)
    n_samples: int = Field(ge=1)

    # Sample/substrate info - identical for every sample of an experiment.
    # The defaulted three keep the sheet's substrate block non-empty (and
    # numeric): present-but-empty columns crash the batch parser.
    substrate_material: str = Field('Glass', min_length=1)
    substrate_conductive_layer: str = Field('ITO', min_length=1)
    number_of_pixels: int = Field(6, ge=1)
    sample_dimension: str | None = None
    sample_area: float | None = None  # cm^2
    pixel_area: float | None = None  # cm^2
    sheet_resistance: float | None = None  # Ohms/square
    transmission: float | None = None  # %
    number_of_junctions: int | None = None

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
    archive: dict
    step_types: list[str]  # the chosen type per step in plan order
    derived_entry: InputCollectionResponse
    sheet_issues: list[str]  # archive fields sheet cannot represent
