from typing import Literal

from nomad.actions.assets.models import ActionAssetRef
from pydantic import BaseModel, Field


class LocalSandWorkflowInput(BaseModel):
    """Input model for the local sand workflow"""

    upload_id: str = Field(
        ...,
        description='Unique identifier for the upload associated with the workflow.',
    )
    user_id: str = Field(
        ..., description='Unique identifier for the user who initiated the workflow.'
    )
    audio_file: ActionAssetRef = Field(
        ...,
        description='Audio recording to transcribe and extract data from.',
        json_schema_extra={
            'x-nomad-widget': 'audio-upload',
            'accept': ['audio/*'],
        },
    )


class TextVerificationDecision(BaseModel):
    """Decision model for human-in-the-loop text verification."""

    decision: Literal['approve', 'reject'] = Field(
        ..., description="The user's decision."
    )
    verified_text: str = Field(
        ..., description='The corrected text to be used for schema extraction'
    )
