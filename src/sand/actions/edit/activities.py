"""The edit job's activities: every one reads what it needs from the
upload and leaves nothing in memory, so Temporal can retry or replay any
of them. The sheet is never patched: each activity rebuilds the state
from the original and the cached proposals (see apply.py)."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from temporalio import activity
from temporalio.exceptions import ApplicationError

from sand.actions import common
from sand.actions.edit.models import (
    ApplyInput,
    EditInput,
    JobInput,
    PrepareInput,
    ReplayInput,
    StoreInput,
    WriteStatusInput,
)
from sand.services.nomad_api import XLSX_CONTENT_TYPE
from sand.sheet_edit.apply import (
    Step,
    apply_all,
    lookup,
    next_seq,
    saved_answer,
)
from sand.sheet_edit.catalog import Catalog
from sand.sheet_edit.changelog import Confirmation
from sand.sheet_edit.edits import ProposedEdits
from sand.sheet_edit.session import (
    CACHE_FILE,
    CHANGELOG_FILE,
    CONFIRMATIONS_FILE,
    FINAL_FILE,
    ORIGINAL_FILE,
    REJECTIONS_FILE,
    STATUS_FILE,
    LlmCache,
    cache_key,
    is_job_cache_file,
    job_cache_file,
    llm_cache_json,
    load_confirmations,
    load_llm_cache,
    read_confirmations,
    read_rejections,
)
from sand.sheet_edit.workbook import sha256

JSON_TYPE = 'application/json'


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
        rejections = await voice.writer.read_raw_file(
            client, data.upload_id, REJECTIONS_FILE
        )
    if original is None:
        raise ApplicationError(
            'no sheet was uploaded for editing in this collection', non_retryable=True
        )
    await _drop_leftover_caches(data)
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
        # a rejected edit is left out of the sheet the later inputs read
        rejected = sorted(read_rejections(rejections))
    except ValueError as why:
        raise ApplicationError(str(why), non_retryable=True) from why
    return {
        'texts': [i.text for i in inputs],
        'input_entry_ids': [i.entry_id for i in inputs],
        'confirmations': confirmed,
        'rejections': rejected,
        'llm_model_name': common.llm_model_name(),
    }


@activity.defn
async def replay_cached_inputs(data: ReplayInput) -> dict:
    """The leading inputs the model already answered on their sheet,
    applied in one go (files read once): how many there were and the
    reports of those after `data.after`. The first input without a cached
    answer stops it; the workflow reads that one and replays again."""
    files = await _read_files(data)
    steps = apply_all(
        files.original,
        data.texts,
        _never_asked,
        set(files.confirmations),
        files.cache.answers,
        cached_only=True,
        rejected=frozenset(data.rejections),
        ids=data.input_ids or None,
    )
    confirmed = set(files.confirmations)
    rejected = set(data.rejections)
    reports = [
        _report(number, entry_id, step, confirmed, rejected, from_cache=True)
        for number, (entry_id, step) in enumerate(zip(data.entry_ids, steps), 1)
        if number > data.after
    ]
    replayed = {'replayed': len(steps), 'reports': reports}
    if data.store_status and len(steps) == len(data.texts):
        # nothing left to ask: the sheet built here is the final one,
        # stored at once (#202)
        earlier = data.store_status.get('inputs', [])[: data.after]
        status = {**data.store_status, 'inputs': [*earlier, *reports]}
        replayed['done'] = await _store(data, files, steps, status)
    return replayed


@activity.defn
async def prepare_input(data: PrepareInput) -> dict:
    """The two prompts for one input, against the sheet the earlier inputs
    left; or nothing to ask, when an answer to these words holds on this
    sheet (apply.py)."""
    from sand.sheet_edit.catalog import build
    from sand.sheet_edit.resolve import prompts

    files = await _read_files(data)
    steps = _apply_inputs_from_cache(files, data.texts[: data.number - 1], data)
    catalog = build(_sheet_before(files, steps))
    if not _to_read(files, data, steps, catalog):
        return {'cached': True, 'prompts': []}
    return {
        'cached': False,
        'prompts': [
            {'system': p.system, 'text': p.text, 'schema': p.schema}
            for p in prompts(_text(data), catalog)
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
    before = _apply_inputs_from_cache(files, data.texts[: data.number - 1], data)
    state = _sheet_before(files, before)
    catalog = build(state)
    if data.answers:
        opinions = [
            Unreadable(reason='unreadable', detail=a.error, said=text)
            if a.error is not None
            else read_answer(a.text, text, catalog)
            for a in data.answers
        ]
        for opinion in opinions:
            if isinstance(opinion, Unreadable):  # the page shows no reason
                activity.logger.warning(
                    'input %s unreadable: %s', data.number, opinion.detail
                )
        agreed = agree(opinions, catalog)
        # what holds only on the sheet the model read is saved here: the
        # `old` check and where the named values sit
        answer = saved_answer(
            text, agreed, catalog, sha256(state), next_seq(files.cache.answers)
        )
        files.cache.add(cache_key(text, state), answer, data.job_id)
    elif _to_read(files, data, before, catalog):
        # the cache file was lost between prepare and apply
        raise ApplicationError(
            f'input {data.number} has no answer and no cached proposal; run again',
            non_retryable=True,
        )
    step = _apply_inputs_from_cache(files, data.texts[: data.number], data)[-1]
    report = _report(
        data.number,
        data.entry_id,
        step,
        set(files.confirmations),
        set(data.rejections),
        from_cache=not data.answers,
    )
    await _write_input(files, data, report)
    return report


async def _write_input(files: '_Files', data: ApplyInput, report: dict) -> None:
    """The job's own cache file (with a new answer) and its status with
    this report: one request, one NOMAD run (#202)."""
    from sand.sheet_edit.resolve import CACHE_VERSION

    outputs = []
    if data.answers:
        # one writer per file, so another job on the upload cannot lose them
        own = llm_cache_json(
            files.cache.of_job(data.job_id), data.llm_model_name, CACHE_VERSION
        )
        outputs.append((job_cache_file(data.job_id), _json(own), JSON_TYPE))
    if data.status:
        status = {
            **data.status,
            'inputs': [*data.status.get('inputs', []), report],
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        outputs.append((STATUS_FILE, _json(status), JSON_TYPE))
    if not outputs:
        return
    voice = common.voice_service()
    async with voice.build_client(common.user_token(data.user_id)) as client:
        await voice.writer.upload_raw_files(
            client,
            data.upload_id,
            [
                (name.rpartition('/')[2], content, ctype)
                for name, content, ctype in outputs
            ],
            directory=STATUS_FILE.rpartition('/')[0],
        )
        # the PUT only queues NOMAD's file update: read back (the reads
        # wait until it is done); a raise retries with the same answers
        for name, content, _ in outputs:
            stored = await voice.writer.read_json(client, data.upload_id, name)
            if stored != json.loads(content):
                raise ApplicationError(
                    f'input {data.number}: NOMAD did not store {name}'
                )


@activity.defn
async def store_final_sheet_and_changelog(data: StoreInput) -> dict:
    """Write the final sheet and the changelog that explains it; the two
    are checked against the original before anything is stored. With the
    shared cache and the completed status (`data.status`) in the same
    request: one NOMAD run, and no sheet without its changelog."""
    files = await _read_files(data)
    steps = _apply_inputs_from_cache(files, data.texts, data)
    return await _store(data, files, steps, data.status)


async def _store(
    data: JobInput, files: '_Files', steps: list[Step], status: dict
) -> dict:
    """The final sheet of `steps`, its changelog, the shared cache and,
    unless `status` is empty, it as completed: in one request."""
    from sand.sheet_edit.catalog import build
    from sand.sheet_edit.changelog import (
        CheckFailed,
        changelog,
        check,
        render,
        to_json,
        written_now,
    )

    final = steps[-1].state if steps else files.original
    log = changelog(files.original, steps, files.confirmations, data.llm_model_name)
    try:
        check(files.original, final, log)
    except CheckFailed as why:
        raise ApplicationError(
            f'the final sheet does not match its changelog: {why}', non_retryable=True
        ) from why
    # a confirmation is bound to the words and the change as shown: an
    # input reworded, or a decision above that changed what it shows,
    # leaves it matching nothing, and the user should be told to look again
    ready = {key for step in steps for key in step.interpretation.ready}
    done = {
        'final': sha256(final),
        'changed': final != files.original,
        'changes': len(log.entries),
        'stale_confirmations': len(set(files.confirmations) - ready),
        'changelog': render(log),
        # the final sheet's catalog names the samples: an edit of the
        # Sample column is in it, a step's catalog is of the sheet before
        'written': written_now(log, build(final)),
    }
    outputs = [
        (FINAL_FILE, final, XLSX_CONTENT_TYPE),
        (CHANGELOG_FILE, to_json(log).encode(), JSON_TYPE),
    ]
    if status:
        completed = {
            **status,
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'phase': 'completed',
            **done,
        }
        outputs.append((STATUS_FILE, _json(completed), JSON_TYPE))
    voice = common.voice_service()
    async with voice.build_client(common.user_token(data.user_id)) as client:
        writer = voice.writer
        # a retry after "completed" landed: a newer job may own the sheet
        # and the status now, and must not be overwritten
        if status and not _ours(
            await writer.read_json(client, data.upload_id, STATUS_FILE), status
        ):
            return done
        shared = await writer.read_json(client, data.upload_id, CACHE_FILE)
        outputs.append(
            (CACHE_FILE, _json(_published(shared, files, data, steps)), JSON_TYPE)
        )
        await writer.upload_raw_files(
            client,
            data.upload_id,
            [
                (name.rpartition('/')[2], content, ctype)
                for name, content, ctype in outputs
            ],
            directory=FINAL_FILE.rpartition('/')[0],
        )
        # the PUT only queues NOMAD's file update: read back (the reads
        # wait until it is done) before the job reports success
        await _check_stored(writer, client, data, final, status)
        # only a job that asked the model has a file of its own; a
        # leftover is harmless, a failure here must not retry the above
        if files.cache.of_job(data.job_id).answers:
            try:
                await writer.delete_raw_file(
                    client, data.upload_id, job_cache_file(data.job_id)
                )
            except Exception:
                activity.logger.warning(
                    'could not delete %s', job_cache_file(data.job_id), exc_info=True
                )
    return done


def _ours(current: dict | None, status: dict) -> bool:
    """Whether the status file is still this job's: its own, or the API's
    reservation made before it started. A newer job's reservation is
    newer than this job's start."""
    if not current:
        return True
    if current.get('job_id') == status.get('job_id'):
        return True
    if current.get('job_id') != 'starting':
        return False
    try:
        reserved = datetime.fromisoformat(current['updated_at'])
        started = datetime.fromisoformat(status['started_at'])
    except (KeyError, TypeError, ValueError):
        # a status of a job started before `started_at`: as before
        return True
    return reserved <= started


async def _check_stored(
    writer, client, data: JobInput, final: bytes, status: dict
) -> None:
    """Raises (Temporal retries) unless the final sheet and, with a
    `status`, the completed status are this job's. A newer job's status
    there means this one's was stored and the lock taken over; the job's
    own reservation still there means it was not."""
    stored = await writer.read_raw_file(client, data.upload_id, FINAL_FILE)
    if status:
        current = await writer.read_json(client, data.upload_id, STATUS_FILE)
        if not _ours(current, status):
            return
        if (current or {}).get('job_id') != data.job_id or current.get(
            'phase'
        ) != 'completed':
            raise ApplicationError('NOMAD did not store the completed status')
    if stored is None or sha256(stored) != sha256(final):
        raise ApplicationError('NOMAD did not store the final sheet')


def _json(payload: dict) -> bytes:
    # as write_json writes it
    return json.dumps(payload, ensure_ascii=False).encode()


@activity.defn
async def write_edit_status(data: WriteStatusInput) -> None:
    voice = common.voice_service()
    async with voice.build_client(common.user_token(data.user_id)) as client:
        if data.status.get('phase') == 'failed' and not _ours(
            await voice.writer.read_json(client, data.upload_id, STATUS_FILE),
            data.status,
        ):
            # this job's end was stored and a newer job took over: its
            # status is not this failure's to replace
            return
        await voice.writer.write_json(client, data.upload_id, STATUS_FILE, data.status)


# --- the upload's files ----------------------------------------------------


@dataclass
class _Files:
    """One activity's view of the upload, read once at its start. The
    confirmations come with the job, not from the file (see JobInput)."""

    original: bytes
    cache: LlmCache
    confirmations: dict[str, Confirmation]


async def _drop_leftover_caches(data: EditInput, job_id: str | None = None) -> None:
    """The cache files of jobs that crashed (#203): one job runs at a
    time, but a job given up for an old status may still run on, so a
    file goes only once the workflows of its answers have ended.
    Best-effort: a file left behind costs nothing but space. `job_id`:
    the running job's (its workflow id) when not given."""
    directory = CACHE_FILE.rpartition('/')[0]
    voice = common.voice_service()
    try:
        own = job_cache_file(job_id or activity.info().workflow_id).rpartition('/')[2]
        async with voice.build_client(common.user_token(data.user_id)) as client:
            names = await voice.writer.list_raw_dir(client, data.upload_id, directory)
            for name in names:
                if not is_job_cache_file(name) or name == own:
                    continue
                path = f'{directory}/{name}'
                cached = await voice.writer.read_json(client, data.upload_id, path)
                jobs = {
                    entry.get('job')
                    for entry in ((cached or {}).get('proposals') or {}).values()
                    if isinstance(entry, dict)
                }
                if all([await _workflow_ended(j) for j in jobs if j]):
                    await voice.writer.delete_raw_file(client, data.upload_id, path)
    except Exception:
        activity.logger.warning('could not drop leftover cache files', exc_info=True)


async def _workflow_ended(workflow_id: str) -> bool:
    """False while Temporal still runs it; raises when Temporal cannot
    tell (the file stays)."""
    from nomad.actions.client import get_client
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError, RPCStatusCode

    client = await get_client()
    try:
        described = await client.get_workflow_handle(workflow_id).describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return True
        raise
    return described.status != WorkflowExecutionStatus.RUNNING


async def _read_files(data: JobInput) -> _Files:
    """The shared cache under the job's own: what this job got wins over
    what earlier jobs published."""
    from sand.sheet_edit.resolve import CACHE_VERSION

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
    cache = load_llm_cache(shared, data.llm_model_name, CACHE_VERSION, data.job_id)
    cache.update(load_llm_cache(own, data.llm_model_name, CACHE_VERSION, data.job_id))
    return _Files(
        original,
        cache,
        load_confirmations(data.confirmations),  # collect checked them
    )


def _published(
    shared: dict | None, files: _Files, data: JobInput, steps: list[Step]
) -> dict:
    """The shared cache with the job's readable answers, for later jobs.
    Two jobs publishing at once can lose each other's entries, which
    costs a later job model calls, not a result."""
    from sand.sheet_edit.resolve import CACHE_VERSION

    merged = load_llm_cache(shared, data.llm_model_name, CACHE_VERSION, data.job_id)
    merged.update(files.cache.of_job(data.job_id, readable_only=True))
    merged = merged.trimmed({step.answer for step in steps})
    return llm_cache_json(merged, data.llm_model_name, CACHE_VERSION)


# --- the state chain ----------------------------------------------------------


def _text(data: PrepareInput) -> str:
    if data.number > len(data.texts):
        raise ApplicationError(
            f'input {data.number} of {len(data.texts)} does not exist',
            non_retryable=True,
        )
    return data.texts[data.number - 1]


def _apply_inputs_from_cache(
    files: _Files, texts: list[str], data: JobInput
) -> list[Step]:
    """The inputs applied in order, every proposal from the cache: the
    model is asked by the workflow, never from here."""
    return apply_all(
        files.original,
        texts,
        _never_asked,
        set(files.confirmations),
        files.cache.answers,
        rejected=frozenset(data.rejections),
        ids=data.input_ids[: len(texts)] or None,
    )


def _to_read(
    files: _Files, data: PrepareInput, before: list[Step], catalog: Catalog
) -> bool:
    """No answer holds on the input's sheet."""
    state = _sheet_before(files, before)
    return lookup(files.cache.answers, _text(data), sha256(state), catalog) is None


def _sheet_before(files: _Files, before: list[Step]) -> bytes:
    """The preview the next input is read on."""
    return before[-1].preview if before else files.original


def _never_asked(text: str, catalog: Catalog) -> ProposedEdits:
    # inputs run in order and each is cached before the next starts, so a
    # miss means the cache file was lost under a running job
    raise ApplicationError(
        f'an earlier input ({text[:60]!r}) has no cached proposal; run again',
        non_retryable=True,
    )


def _report(  # noqa: PLR0913
    number: int,
    entry_id: str,
    step: Step,
    confirmed: set[str],
    rejected: set[str],
    *,
    from_cache: bool,
) -> dict:
    """One input as the GUI shows it: the preview, and each edit that may
    be confirmed with its key."""
    from sand.sheet_edit.ground import Grounded, Refused
    from sand.sheet_edit.preview import edit_lines, edit_summary

    catalog = step.interpretation.catalog
    results = step.interpretation.results
    written = {(c.row, c.column) for c in step.written if c.field == 'value'}
    return {
        'input': number,
        'entry_id': entry_id,
        'text': step.text,
        'from_cache': from_cache,
        'preview': step.interpretation.preview,
        'ready': [
            {
                'key': key,
                'line': edit_lines(edit, catalog)[0],
                **edit_summary(edit, catalog),
                # word checks: shown on the edit
                'checks': list(edit.checks),
            }
            for key, edit in step.interpretation.ready.items()
        ],
        # written; `held` waits for the inputs above (apply.py)
        'confirmed': [key for key in step.confirmed if key not in step.held],
        'held': list(step.held),
        # as this run knew them: one rejected or taken back since changes
        # the preview of every input after it
        'rejected': [key for key in step.interpretation.ready if key in rejected],
        # what the job could not settle (uncertain, ambiguous, not found)
        'unresolved': len(step.interpretation.proposal.unresolved),
        # edits a check refused, and edits the sheet already holds
        'not_applied': sum(isinstance(r, Refused) for r in results),
        'unchanged': sum(isinstance(r, Grounded) and not r.rows for r in results),
        'cells_written': len(written),
        'refused': step.refused,
    }
