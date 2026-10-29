from temporalio import activity
from temporalio.exceptions import ApplicationError

from sand.actions import common
from sand.actions.extract.models import ExtractInput, StoreInput, WriteStatusInput


@activity.defn
async def collect_and_route(data: ExtractInput) -> dict:
    """The experiment's routed inputs plus everything the workflow needs
    to build the LLM child-workflow inputs (schemas, model config).

    Input problems (untranscribed audio, bad form) are the user's to fix,
    not transient: non-retryable."""
    from sand.hysprint.generate import HysprintInputError, route_inputs
    from sand.hysprint.steps import select_schema

    voice = common.voice_service()
    async with voice.build_client(common.user_token(data.user_id)) as client:
        inputs = await voice.collect_inputs(
            client, data.upload_id, collection_entry_id=data.collection_entry_id
        )

    # todo: decide if this warns or error
    pending = [i for i in inputs if i.text is None]
    if pending:
        raise ApplicationError(
            f'{len(pending)} input(s) not transcribed or processed yet; '
            'try again in a moment',
            non_retryable=True,
        )
    try:
        info, step_texts = route_inputs(inputs, data.upload_id)
    except HysprintInputError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc

    # No API key here, we can set the key in the env for the child workflow
    # since this is only for hysrpint lab.
    return {
        'info': info,
        'step_texts': step_texts,
        'select_schema': select_schema(),
        'input_entry_ids': [i.entry_id for i in inputs],
        'llm_model_name': common.llm_model_name(),
    }


@activity.defn
def make_fill_schema(step_type: str) -> dict:
    from sand.hysprint.steps import fill_schema

    try:
        return fill_schema(step_type)
    except ValueError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc


@activity.defn
async def assemble_and_store(data: StoreInput) -> dict:
    """Slots + form -> archive -> sheet -> upload, reparse, relink."""
    from sand.hysprint.generate import assemble
    from sand.hysprint.sheet import grid_to_xlsx_bytes, to_sheet
    from sand.hysprint.sheet_store import SheetStore

    try:
        archive = assemble(data.info, [dict(slot) for slot in data.slots])
    except ValueError as exc:
        # e.g. a narration names a sample label the form did not declare
        raise ApplicationError(str(exc), non_retryable=True) from exc

    grid, sheet_issues = to_sheet(archive)
    xlsx = grid_to_xlsx_bytes(grid)
    voice = common.voice_service()
    async with voice.build_client(common.user_token(data.user_id)) as client:
        handle, replaced_edits = await SheetStore(voice).store_extracted(
            client,
            data.upload_id,
            xlsx,
            data.collection_entry_id,
            archive=archive,
            input_entry_ids=data.input_entry_ids,
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

    voice = common.voice_service()
    async with voice.build_client(common.user_token(data.user_id)) as client:
        await voice.writer.write_json(
            client, data.upload_id, EXTRACTION_STATUS_MAINFILE, data.status
        )
