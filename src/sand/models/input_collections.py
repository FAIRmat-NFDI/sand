from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator


class HysprintExperimentInfoForm(BaseModel):
    project_name: str = Field(min_length=1)
    batch: str = Field(min_length=1)
    subbatch: str = Field(min_length=1)
    first_sample: str = Field(min_length=1)
    n_samples: int = Field(ge=1)

    # same for the samples belonging to the same experiment
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
    created: str | None = None  # when the experiment was made, ISO 8601


class InputCollectionListResponse(BaseModel):
    input_collections: list[InputCollectionSummaryModel]


class RenameRequest(BaseModel):
    name: str  # the sheet's name on the page and in NOMAD


class CreateNoteRequest(BaseModel):
    text: str
    label: str = ''  # optional free text for the user


WrongReason = Literal['column', 'samples', 'value', 'other']


class WrongEdit(BaseModel):
    """A proposed edit the user says is wrong, by its confirmation key."""

    key: str = Field(min_length=1, max_length=200)
    reason: WrongReason


class FeedbackRequest(BaseModel):
    what_wrong: str = Field('', max_length=5000)
    expected: str = Field('', max_length=5000)
    page: str = Field('', max_length=200)
    user_agent: str = Field('', max_length=500)
    # what the page showed or failed at lately, newest last
    recent_errors: list[Annotated[str, Field(max_length=1000)]] = Field(
        default_factory=list, max_length=20
    )
    edit: WrongEdit | None = None

    @model_validator(mode='after')
    def _says_something(self):
        # an edit report's reason is enough; a page report needs words
        if self.edit is None and not self.what_wrong.strip():
            raise ValueError("say what's not working")
        return self


class FeedbackResponse(BaseModel):
    file: str


class HysprintExtractResponse(BaseModel):
    archive: dict
    step_types: list[str]
    derived_entry: InputCollectionResponse
    sheet_issues: list[str]  # archive fields sheet cannot represent
    warnings: list[str] = []  # e.g. a hand-edited sheet was replaced


class InputItemModel(BaseModel):
    entry_id: str
    entry_url: str
    kind: str  # 'audio' | 'note'
    label: str
    datetime: str | None
    text: str | None  # effective text (intended > corrected > transcript)
    corrected: bool  # a human revision exists
    status: str | None  # audio transcription status; None for notes
    deletable: bool  # False for the experiment-info form: extraction needs it
    applied: bool = False  # an edit of it was confirmed


class InputListResponse(BaseModel):
    inputs: list[InputItemModel]


class ReviseInputRequest(BaseModel):
    text: str


class ReviseInputResponse(BaseModel):
    kind: str  # which field was written: audio -> corrected_transcript, note -> text


class ReviseLabelRequest(BaseModel):
    label: str  # empty clears the label


class ReviseDatetimeRequest(BaseModel):
    datetime: str  # ISO 8601; reorders the input on the experiment timeline


class JobResponse(BaseModel):
    job_id: str  # the action workflow id; poll the job's status endpoint


class ConfirmEditsRequest(BaseModel):
    keys: list[str]  # from the reports' ready edits (GET .../edits)


class ConfirmEditsResponse(BaseModel):
    confirmed: list[str]  # every confirmed key, the new ones included


class RejectEditsRequest(BaseModel):
    keys: list[str]  # ready edits of the last job, none confirmed
    reason: WrongReason | None = None  # optional, as in the "Wrong" dialog
    meant: str = Field('', max_length=5000)


class RejectEditsResponse(BaseModel):
    rejected: list[str]  # every rejected key, the new ones included


class UndoRejectRequest(BaseModel):
    keys: list[str]


class SheetUploadResponse(BaseModel):
    # False: uploaded bytes equal the stored sheet; the derived links are
    # still verified (and a failed previous parse repaired) either way.
    changed: bool
    derived_entry: InputCollectionResponse
