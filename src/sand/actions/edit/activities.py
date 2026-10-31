"""The edit job's activities: every one reads what it needs from the
upload and leaves nothing in memory, so Temporal can retry or replay any
of them. The sheet is never patched: each activity rebuilds the state
from the original and the cached proposals (see apply.py)."""

from dataclasses import dataclass

from temporalio import activity
from temporalio.exceptions import ApplicationError

from sand.actions import common
from sand.actions.edit.models import (
    ApplyInput,
    EditInput,
    JobInput,
    PrepareInput,
    WriteStatusInput,
)
from sand.services.nomad_api import XLSX_CONTENT_TYPE
from sand.sheet_edit.apply import Step, apply_all
from sand.sheet_edit.catalog import Catalog
from sand.sheet_edit.changelog import Confirmation
from sand.sheet_edit.edits import ProposedEdits
from sand.sheet_edit.session import (
    CACHE_FILE,
    CONFIRMATIONS_FILE,
    FINAL_FILE,
    ORIGINAL_FILE,
    STATUS_FILE,
    LlmCache,
    cache_key,
    job_cache_file,
    llm_cache_json,
    load_confirmations,
    load_llm_cache,
    read_confirmations,
)


@activity.defn
async def collect_edit_inputs(data: EditInput) -> dict:
    """The collection's inputs in order, the confirmations so far and the
    model to ask. Input problems are the user's to fix: non-retryable."""
    voice = common.voice_service()
    async with voice.build_client(common.user_token(data.user_id)) as client:
        inputs = await voice.collect_inputs(
            client, data.upload_id, collection_entry_id=data.collection_entry_id
        )
        original = await voice.writer.read_raw_file(
            client, data.upload_id, ORIGINAL_FILE
        )
        confirmations = await voice.writer.read_raw_file(
            client, data.upload_id, CONFIRMATIONS_FILE
        )
    if original is None:
        raise ApplicationError(
            'no sheet was uploaded for editing in this collection', non_retryable=True
        )
    pending = [i for i in inputs if i.text is None]
    if pending:
        raise ApplicationError(
            f'{len(pending)} input(s) not transcribed or processed yet; '
            'try again in a moment',
            non_retryable=True,
        )
    try:
        confirmed = read_confirmations(confirmations)
        load_confirmations(confirmed)  # malformed entries stop the job here
    except ValueError as why:
        raise ApplicationError(str(why), non_retryable=True) from why
    return {
        'texts': [i.text for i in inputs],
        'input_entry_ids': [i.entry_id for i in inputs],
        'confirmations': confirmed,
        'llm_model_name': common.llm_model_name(),
    }


@activity.defn
async def prepare_input(data: PrepareInput) -> dict:
    """The two prompts for one input, against the sheet the earlier inputs
    left; or nothing to ask, when the model already answered these words
    on this sheet."""
    from sand.sheet_edit.catalog import build
    from sand.sheet_edit.resolve import prompts

    files = await _read_files(data)
    text = _text(data)
    state = _state_before(files, data)
    if cache_key(text, state) in files.cache.proposals:
        return {'cached': True, 'prompts': []}
    return {
        'cached': False,
        'prompts': [
            {'system': p.system, 'text': p.text, 'schema': p.schema}
            for p in prompts(text, build(state))
        ],
    }


@activity.defn
async def agree_cache_and_apply_input(data: ApplyInput) -> dict:
    """Agree the answers, cache the proposal, apply the input (its
    confirmed edits are written) and report what the user sees."""
    from sand.sheet_edit.catalog import build
    from sand.sheet_edit.edits import Unreadable
    from sand.sheet_edit.resolve import agree, read_answer

    files = await _read_files(data)
    text = _text(data)
    state = _state_before(files, data)
    key = cache_key(text, state)
    if data.answers:
        catalog = build(state)
        opinions = [
            Unreadable(reason='unreadable', detail=a.error, said=text)
            if a.error is not None
            else read_answer(a.text, text, catalog)
            for a in data.answers
        ]
        files.cache.add(key, agree(opinions, catalog), data.job_id)
        await _write_cache(files, data)
    elif key not in files.cache.proposals:
        # the cache file was lost between prepare and apply
        raise ApplicationError(
            f'input {data.number} has no answer and no cached proposal; run again',
            non_retryable=True,
        )
    step = _apply_inputs_from_cache(files, data.texts[: data.number])[-1]
    return _report(data, step, set(files.confirmations), from_cache=not data.answers)


@activity.defn
async def store_final_sheet_and_changelog(data: JobInput) -> dict:
    """Write the final sheet and the changelog that explains it; the two
    are checked against the original before anything is stored."""
    from sand.sheet_edit.changelog import CheckFailed, changelog, check, render
    from sand.sheet_edit.workbook import sha256

    files = await _read_files(data)
    steps = _apply_inputs_from_cache(files, data.texts)
    final = steps[-1].state if steps else files.original
    log = changelog(files.original, steps, files.confirmations, data.llm_model_name)
    try:
        check(files.original, final, log)
    except CheckFailed as why:
        raise ApplicationError(
            f'the final sheet does not match its changelog: {why}', non_retryable=True
        ) from why
    voice = common.voice_service()
    async with voice.build_client(common.user_token(data.user_id)) as client:
        await voice.writer.upload_raw_file(
            client, data.upload_id, FINAL_FILE, final, XLSX_CONTENT_TYPE
        )
        await _publish_cache(voice.writer, client, files, data)
    # a confirmation is bound to the words, the sheet and the change: an
    # input reworded or an earlier edit confirmed since leaves it matching
    # nothing, and the user should be told to look again
    ready = {key for step in steps for key in step.interpretation.ready}
    return {
        'final': sha256(final),
        'changed': final != files.original,
        'changes': len(log.entries),
        'stale_confirmations': len(set(files.confirmations) - ready),
        'changelog': render(log),
    }


@activity.defn
async def write_edit_status(data: WriteStatusInput) -> None:
    voice = common.voice_service()
    async with voice.build_client(common.user_token(data.user_id)) as client:
        await voice.writer.write_json(client, data.upload_id, STATUS_FILE, data.status)


# --- the upload's files ----------------------------------------------------


@dataclass
class _Files:
    """One activity's view of the upload, read once at its start. The
    confirmations come with the job, not from the file (see JobInput)."""

    original: bytes
    cache: LlmCache
    confirmations: dict[str, Confirmation]


async def _read_files(data: JobInput) -> _Files:
    """The shared cache under the job's own: what this job got wins over
    what earlier jobs published."""
    from sand.sheet_edit.resolve import PROMPT_VERSION

    voice = common.voice_service()
    async with voice.build_client(common.user_token(data.user_id)) as client:
        original = await voice.writer.read_raw_file(
            client, data.upload_id, ORIGINAL_FILE
        )
        shared = await voice.writer.read_json(client, data.upload_id, CACHE_FILE)
        own = await voice.writer.read_json(
            client, data.upload_id, job_cache_file(data.job_id)
        )
    if original is None:
        raise ApplicationError(
            'the sheet to edit is gone from the collection', non_retryable=True
        )
    cache = load_llm_cache(shared, data.llm_model_name, PROMPT_VERSION, data.job_id)
    cache.update(load_llm_cache(own, data.llm_model_name, PROMPT_VERSION, data.job_id))
    return _Files(
        original,
        cache,
        load_confirmations(data.confirmations),  # collect checked them
    )


async def _write_cache(files: _Files, data: JobInput) -> None:
    """The job's own file, this job's entries only: one writer, so
    another job on the same upload cannot lose them."""
    from sand.sheet_edit.resolve import PROMPT_VERSION

    voice = common.voice_service()
    async with voice.build_client(common.user_token(data.user_id)) as client:
        await voice.writer.write_json(
            client,
            data.upload_id,
            job_cache_file(data.job_id),
            llm_cache_json(
                files.cache.of_job(data.job_id), data.llm_model_name, PROMPT_VERSION
            ),
        )


async def _publish_cache(writer, client, files: _Files, data: JobInput) -> None:
    """The job's readable answers into the shared cache, for later jobs;
    its own file goes. Two jobs publishing at once can lose each other's
    entries here, which costs a later job model calls, not a result."""
    from sand.sheet_edit.resolve import PROMPT_VERSION

    shared = await writer.read_json(client, data.upload_id, CACHE_FILE)
    merged = load_llm_cache(shared, data.llm_model_name, PROMPT_VERSION, data.job_id)
    merged.update(files.cache.of_job(data.job_id, readable_only=True))
    await writer.write_json(
        client,
        data.upload_id,
        CACHE_FILE,
        llm_cache_json(merged, data.llm_model_name, PROMPT_VERSION),
    )
    await writer.delete_raw_file(client, data.upload_id, job_cache_file(data.job_id))


# --- the state chain ----------------------------------------------------------


def _text(data: PrepareInput) -> str:
    if data.number > len(data.texts):
        raise ApplicationError(
            f'input {data.number} of {len(data.texts)} does not exist',
            non_retryable=True,
        )
    return data.texts[data.number - 1]


def _apply_inputs_from_cache(files: _Files, texts: list[str]) -> list[Step]:
    """The inputs applied in order, every proposal from the cache: the
    model is asked by the workflow, never from here."""
    return apply_all(
        files.original,
        texts,
        _never_asked,
        set(files.confirmations),
        files.cache.proposals,
    )


def _state_before(files: _Files, data: PrepareInput) -> bytes:
    steps = _apply_inputs_from_cache(files, data.texts[: data.number - 1])
    return steps[-1].state if steps else files.original


def _never_asked(text: str, catalog: Catalog) -> ProposedEdits:
    # inputs run in order and each is cached before the next starts, so a
    # miss means the cache file was lost under a running job
    raise ApplicationError(
        f'an earlier input ({text[:60]!r}) has no cached proposal; run again',
        non_retryable=True,
    )


def _report(
    data: ApplyInput, step: Step, confirmed: set[str], from_cache: bool
) -> dict:
    """One input as the GUI shows it: the preview, and each edit that may
    be confirmed with its key."""
    from sand.sheet_edit.preview import edit_lines

    catalog = step.interpretation.catalog
    written = {(c.row, c.column) for c in step.written if c.field == 'value'}
    return {
        'input': data.number,
        'entry_id': data.entry_id,
        'text': step.text,
        'from_cache': from_cache,
        'preview': step.interpretation.preview,
        'ready': [
            {'key': key, 'line': edit_lines(edit, catalog)[0]}
            for key, edit in step.interpretation.ready.items()
        ],
        'confirmed': [key for key in step.interpretation.ready if key in confirmed],
        'cells_written': len(written),
        'refused': step.refused,
    }
