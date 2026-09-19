"""Worker-side pieces of the extract action.

NOMAD I/O activities mint a short-lived token for the requesting user
(generate_simple_token - the mechanism behind NOMAD's app tokens) and
reuse the exact HTTP service code the API endpoints use, so the worker
needs no second implementation of the collect/write logic. The pure
pipeline steps that read packaged files (schema slicing, the sheet
template) also run here: workflow code must stay deterministic and free
of file I/O.
"""

import hashlib
from datetime import datetime, timezone

from temporalio import activity
from temporalio.exceptions import ApplicationError

from sand.actions.extract.models import ExtractInput, StoreInput, WriteStatusInput

TOKEN_EXPIRES_S = 15 * 60


def _voice_service():
    from nomad.config import config

    from sand.services.voice_eln import VoiceElnService

    entry_point = config.get_plugin_entry_point('sand.apis:sand_api')
    return VoiceElnService(base_url=entry_point.nomad_base_url)


def _user_token(user_id: str) -> str:
    from nomad.auth.tokens import generate_simple_token

    return generate_simple_token(user_id, TOKEN_EXPIRES_S)


@activity.defn
async def collect_and_route(data: ExtractInput) -> dict:
    """The experiment's routed inputs plus everything the workflow needs
    to build the LLM child-workflow inputs (schemas, model config).

    Input problems (untranscribed audio, bad form) are the user's to fix,
    not transient: non-retryable."""
    from nomad.config import config

    from sand.hysprint.generate import HysprintInputError, route_inputs
    from sand.hysprint.steps import select_schema

    voice = _voice_service()
    async with voice.build_client(_user_token(data.user_id)) as client:
        inputs = await voice.collect_inputs(
            client, data.upload_id, collection_entry_id=data.collection_entry_id
        )

    pending = [i for i in inputs if i.text is None]
    if pending:
        raise ApplicationError(
            f'{len(pending)} input(s) not transcribed or processed yet; '
            'try again in a moment',
            non_retryable=True,
        )
    try:
        info, step_texts = route_inputs(inputs)
    except HysprintInputError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc

    entry_point = config.get_plugin_entry_point('sand.apis:sand_api')
    return {
        'info': info,
        'step_texts': step_texts,
        'select_schema': select_schema(),
        'input_entry_ids': [i.entry_id for i in inputs],
        'llm_model_name': entry_point.llm_model_name,
        'llm_api_key': entry_point.llm_api_key,
    }


@activity.defn
def make_fill_schema(step_type: str) -> dict:
    """The FILL schema slice for one step type (reads the schema artifact,
    so it lives in an activity, not in workflow code)."""
    from sand.hysprint.steps import fill_schema

    try:
        return fill_schema(step_type)
    except ValueError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc


@activity.defn
async def assemble_and_store(data: StoreInput) -> dict:
    """Slots + form -> archive -> sheet -> upload, reparse, relink.

    Safe to retry: add_derived_sheet deletes the previous parse output
    and rewrites everything (issue #34's always-regenerate)."""
    from sand.hysprint.generate import assemble
    from sand.hysprint.sheet import (
        DERIVED_SHEET_MAINFILE,
        EXTRACTED_JSON_MAINFILE,
        grid_to_xlsx_bytes,
        to_sheet,
    )
    from sand.services.voice_eln import DerivedSheet

    try:
        archive = assemble(data.info, [dict(slot) for slot in data.slots])
    except ValueError as exc:
        # e.g. a narration names a sample label the form did not declare
        raise ApplicationError(str(exc), non_retryable=True) from exc

    grid, sheet_issues = to_sheet(archive)
    xlsx = grid_to_xlsx_bytes(grid)
    sheet = DerivedSheet(
        xlsx=xlsx,
        xlsx_mainfile=DERIVED_SHEET_MAINFILE,
        extraction={
            'archive': archive,
            'xlsx_sha256': hashlib.sha256(xlsx).hexdigest(),
            'extracted_at': datetime.now(timezone.utc).isoformat(),
            'input_entry_ids': data.input_entry_ids,
        },
        extraction_mainfile=EXTRACTED_JSON_MAINFILE,
    )

    voice = _voice_service()
    async with voice.build_client(_user_token(data.user_id)) as client:
        handle, replaced_edits = await voice.add_derived_sheet(
            client,
            data.upload_id,
            sheet,
            collection_entry_id=data.collection_entry_id,
        )

    warnings = []
    if replaced_edits:
        warnings.append(
            'the sheet had manual edits (it did not match the recorded '
            'hash); this extraction replaced them'
        )
    return {
        'derived_entry_id': handle.entry_id,
        'step_types': [slot['step_type'] for slot in data.slots],
        'sheet_issues': sheet_issues,
        'warnings': warnings,
    }


@activity.defn
async def write_extraction_status(data: WriteStatusInput) -> None:
    """Write the status file into the experiment upload (the GUI polls it)."""
    from sand.hysprint.sheet import EXTRACTION_STATUS_MAINFILE

    voice = _voice_service()
    async with voice.build_client(_user_token(data.user_id)) as client:
        await voice.write_status_file(
            client, data.upload_id, EXTRACTION_STATUS_MAINFILE, data.status
        )
