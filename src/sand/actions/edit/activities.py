"""The edit job's activities: every one reads what it needs from the
upload and leaves nothing in memory, so Temporal can retry or replay any
of them. The sheet is never patched: each activity rebuilds the state
from the original and the cached proposals (see apply.py)."""

from temporalio import activity
from temporalio.exceptions import ApplicationError

from sand.actions.edit.models import (
    EditInput,
    FinishInput,
    JobInput,
    PrepareInput,
    WriteStatusInput,
)
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
    llm_cache_json,
    load_confirmations,
    load_llm_cache,
)

TOKEN_EXPIRES_S = 15 * 60
XLSX_CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def _voice_service():
    from nomad.config import config

    from sand.services.voice_eln import VoiceElnService

    entry_point = config.get_plugin_entry_point('sand.apis:sand_api')
    return VoiceElnService(base_url=entry_point.nomad_base_url)


def _user_token(user_id: str) -> str:
    from nomad.auth.tokens import generate_simple_token

    return generate_simple_token(user_id, TOKEN_EXPIRES_S)


def _llm_model_name() -> str:
    from nomad.config import config

    return config.get_plugin_entry_point('sand.apis:sand_api').llm_model_name


@activity.defn
async def collect_edit_inputs(data: EditInput) -> dict:
    """The collection's inputs in order, and the model to ask. Input
    problems are the user's to fix: non-retryable."""
    voice = _voice_service()
    async with voice.build_client(_user_token(data.user_id)) as client:
        inputs = await voice.collect_inputs(
            client, data.upload_id, collection_entry_id=data.collection_entry_id
        )
        original = await voice.writer.read_raw_file(
            client, data.upload_id, ORIGINAL_FILE
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
    return {
        'texts': [i.text for i in inputs],
        'input_entry_ids': [i.entry_id for i in inputs],
        'llm_model_name': _llm_model_name(),
    }


@activity.defn
async def prepare_input(data: PrepareInput) -> dict:
    """The two prompts for one input, against the sheet the earlier inputs
    left; or nothing to ask, when the model already answered these words
    on this sheet."""
    from sand.sheet_edit.catalog import build
    from sand.sheet_edit.resolve import prompts

    files = await _read_files(data)
    state = _state_before(files, data)
    if cache_key(_text(data), state) in files.cache.proposals:
        return {'cached': True, 'prompts': []}
    return {
        'cached': False,
        'prompts': [
            {'system': p.system, 'text': p.text, 'schema': p.schema}
            for p in prompts(_text(data), build(state))
        ],
    }


@activity.defn
async def finish_input(data: FinishInput) -> dict:
    """Agree the answers, cache the proposal, apply the input (its
    confirmed edits are written) and report what the user sees."""
    from sand.sheet_edit.catalog import build
    from sand.sheet_edit.edits import Unreadable
    from sand.sheet_edit.resolve import agree, read_answer

    files = await _read_files(data)
    state = _state_before(files, data)
    text = _text(data)
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
        # the cache file was lost between prepare and finish
        raise ApplicationError(
            f'input {data.number} has no answer and no cached proposal; run again',
            non_retryable=True,
        )
    step = _replay(files, data.texts[: data.number])[-1]
    return _report(data, step, set(files.confirmations), from_cache=not data.answers)


@activity.defn
async def finish_job(data: JobInput) -> dict:
    """Write the final sheet and the changelog that explains it; the two
    are checked against the original before anything is stored."""
    from sand.sheet_edit.changelog import CheckFailed, changelog, check, render
    from sand.sheet_edit.workbook import sha256

    files = await _read_files(data)
    steps = _replay(files, data.texts)
    final = steps[-1].state if steps else files.original
    log = changelog(files.original, steps, files.confirmations, data.llm_model_name)
    try:
        check(files.original, final, log)
    except CheckFailed as why:
        raise ApplicationError(
            f'the final sheet does not match its changelog: {why}', non_retryable=True
        ) from why
    voice = _voice_service()
    async with voice.build_client(_user_token(data.user_id)) as client:
        await voice.writer.upload_raw_file(
            client, data.upload_id, FINAL_FILE, final, XLSX_CONTENT_TYPE
        )
    return {
        'final': sha256(final),
        'changed': final != files.original,
        'changes': len(log.entries),
        'changelog': render(log),
    }


@activity.defn
async def write_edit_status(data: WriteStatusInput) -> None:
    voice = _voice_service()
    async with voice.build_client(_user_token(data.user_id)) as client:
        await voice.writer.write_json(client, data.upload_id, STATUS_FILE, data.status)


# --- the upload's files ----------------------------------------------------


class _Files:
    """One activity's view of the upload: read once, at its start."""

    def __init__(
        self, original: bytes, cache: LlmCache, confirmations: dict[str, Confirmation]
    ):
        self.original = original
        self.cache = cache
        self.confirmations = confirmations


async def _read_files(data: JobInput) -> _Files:
    from sand.sheet_edit.resolve import PROMPT_VERSION

    voice = _voice_service()
    async with voice.build_client(_user_token(data.user_id)) as client:
        original = await voice.writer.read_raw_file(
            client, data.upload_id, ORIGINAL_FILE
        )
        cache = await voice.writer.read_json(client, data.upload_id, CACHE_FILE)
        confirmations = await voice.writer.read_json(
            client, data.upload_id, CONFIRMATIONS_FILE
        )
    if original is None:
        raise ApplicationError(
            'the sheet to edit is gone from the collection', non_retryable=True
        )
    try:
        confirmed = load_confirmations(confirmations)
    except ValueError as why:
        raise ApplicationError(str(why), non_retryable=True) from why
    return _Files(
        original,
        load_llm_cache(cache, data.llm_model_name, PROMPT_VERSION, data.job_id),
        confirmed,
    )


async def _write_cache(files: _Files, data: JobInput) -> None:
    from sand.sheet_edit.resolve import PROMPT_VERSION

    voice = _voice_service()
    async with voice.build_client(_user_token(data.user_id)) as client:
        await voice.writer.write_json(
            client,
            data.upload_id,
            CACHE_FILE,
            llm_cache_json(files.cache, data.llm_model_name, PROMPT_VERSION),
        )


# --- the state chain ----------------------------------------------------------


def _text(data: PrepareInput) -> str:
    return data.texts[data.number - 1]


def _replay(files: _Files, texts: list[str]) -> list[Step]:
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
    steps = _replay(files, data.texts[: data.number - 1])
    return steps[-1].state if steps else files.original


def _never_asked(text: str, catalog: Catalog) -> ProposedEdits:
    # inputs run in order and each is cached before the next starts, so a
    # miss means the cache file was lost under a running job
    raise ApplicationError(
        f'an earlier input ({text[:60]!r}) has no cached proposal; run again',
        non_retryable=True,
    )


def _report(
    data: FinishInput, step: Step, confirmed: set[str], from_cache: bool
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
