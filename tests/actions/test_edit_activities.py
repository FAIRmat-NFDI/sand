"""The edit activities against a fake upload: a dict of raw files."""

import contextlib
import json
from dataclasses import dataclass, field

import pytest
from temporalio.exceptions import ApplicationError

from sand.actions import common
from sand.actions.edit import activities
from sand.actions.edit.models import (
    ApplyInput,
    EditInput,
    JobInput,
    LlmAnswer,
    PrepareInput,
    ReplayInput,
    StoreInput,
    WriteStatusInput,
)
from sand.services.voice_eln import CollectedInput
from sand.sheet_edit import changelog
from sand.sheet_edit.catalog import build
from sand.sheet_edit.edits import proposed_edits_schema
from sand.sheet_edit.resolve import CACHE_VERSION, system_prompt
from sand.sheet_edit.session import (
    CACHE_FILE,
    CHANGELOG_FILE,
    CONFIRMATIONS_FILE,
    FINAL_FILE,
    ORIGINAL_FILE,
    STATUS_FILE,
    job_cache_file,
)
from sand.sheet_edit.workbook import sha256

pytestmark = pytest.mark.asyncio

MODEL = 'gemini/test'
STARTED = '2026-11-18T10:00:00+00:00'  # the job's start
SPEED = 'C025'
SAID = 'speed 4000 for samples 1 to 3'
SAMPLES_1_TO_3 = 3  # cells one confirmed edit of SAID writes
SPEED_EDIT = {
    'target': SPEED,
    'rows': {'first': '1', 'last': '3', 'subbatch': None},
    'anchor': None,
    'old': '3000',
    'unit': None,
    'said_old': None,
    'new': '4000',
    'said': SAID,
}


def answer(*edits, unresolved=()):
    return LlmAnswer(
        text=json.dumps({'edits': list(edits), 'unresolved': list(unresolved)})
    )


@dataclass
class FakeWriter:
    files: dict[str, bytes]
    written: list[str] = field(default_factory=list)
    requests: int = 0  # PUTs and DELETEs: each one a NOMAD run

    async def read_raw_file(self, client, upload_id, name, *, wait=True):
        return self.files.get(name)

    async def read_json(self, client, upload_id, name):
        raw = self.files.get(name)
        return None if raw is None else json.loads(raw)

    async def upload_raw_file(self, client, upload_id, name, content, content_type):
        self.files[name] = content
        self.written.append(name)
        self.requests += 1

    async def upload_raw_files(self, client, upload_id, files, directory=''):
        for name, content, _ in files:
            path = f'{directory}/{name}' if directory else name
            self.files[path] = content
            self.written.append(path)
        self.requests += 1

    async def write_json(self, client, upload_id, name, payload):
        await self.upload_raw_file(
            client, upload_id, name, json.dumps(payload).encode(), 'application/json'
        )

    async def delete_raw_file(self, client, upload_id, name):
        self.files.pop(name, None)
        self.requests += 1


@dataclass
class FakeVoice:
    writer: FakeWriter
    inputs: list[CollectedInput]

    def build_client(self, token):
        return contextlib.nullcontext(object())

    async def collect_inputs(self, client, upload_id, collection_entry_id):
        return self.inputs


def collected(entry_id: str, text: str | None) -> CollectedInput:
    return CollectedInput(
        entry_id=entry_id, kind='note', text=text, label='', datetime=None
    )


@pytest.fixture
def upload(synthetic_xlsx, monkeypatch):
    """A collection with the synthetic sheet and two inputs."""
    voice = FakeVoice(
        FakeWriter({ORIGINAL_FILE: synthetic_xlsx}),
        [collected('e1', SAID), collected('e2', 'clear solvent 2 of sample 1')],
    )
    monkeypatch.setattr(common, 'voice_service', lambda: voice)
    monkeypatch.setattr(common, 'user_token', lambda user_id: 'token')
    monkeypatch.setattr(common, 'llm_model_name', lambda: MODEL)
    return voice


def job(upload, job_id='job-1') -> StoreInput:
    """As the workflow builds it from `collect_edit_inputs`: the
    confirmations file as it is now. A StoreInput without a status, so
    the store takes it as well."""
    raw = upload.writer.files.get(CONFIRMATIONS_FILE)
    return StoreInput(
        upload_id='u1',
        user_id='user',
        job_id=job_id,
        llm_model_name=MODEL,
        texts=[i.text for i in upload.inputs],
        confirmations={} if raw is None else json.loads(raw),
        input_ids=[i.entry_id for i in upload.inputs],
    )


def prepare(upload, number, job_id='job-1', snapshot=None) -> PrepareInput:
    collected = snapshot or job(upload, job_id)
    return PrepareInput(**collected.model_dump(), number=number)


def apply(upload, number, answers, job_id='job-1', snapshot=None) -> ApplyInput:
    collected = snapshot or job(upload, job_id)
    return ApplyInput(
        **collected.model_dump(),
        number=number,
        entry_id=upload.inputs[number - 1].entry_id,
        answers=answers,
    )


def replay(upload, job_id='job-1') -> ReplayInput:
    return ReplayInput(
        **job(upload, job_id).model_dump(),
        entry_ids=[i.entry_id for i in upload.inputs],
    )


def rejecting(snapshot: JobInput, keys: list[str]) -> JobInput:
    return snapshot.model_copy(update={'rejections': keys})


JOB_CACHE = job_cache_file('job-1')


def cache_file(upload, name=JOB_CACHE) -> dict:
    return json.loads(upload.writer.files[name])


# --- collect ---------------------------------------------------------------------


async def test_collect_gives_the_inputs_in_order_the_confirmations_and_the_model(
    upload,
):
    data = EditInput(upload_id='u1', user_id='user', collection_entry_id='c1')
    result = await activities.collect_edit_inputs(data)
    assert result == {
        'texts': [SAID, 'clear solvent 2 of sample 1'],
        'input_entry_ids': ['e1', 'e2'],
        'confirmations': {},
        'rejections': [],
        'llm_model_name': MODEL,
    }
    await confirm(upload, 'k1')
    result = await activities.collect_edit_inputs(data)
    assert result['confirmations'] == {'k1': CONFIRMED}


@pytest.mark.parametrize(
    'raw', [b'{"k1": {"by": "tester", "at": "2026', b'[]', b'{"k1": {"by": "tester"}}']
)
async def test_collect_refuses_a_damaged_confirmations_file(upload, raw):
    upload.writer.files[CONFIRMATIONS_FILE] = raw
    with pytest.raises(ApplicationError, match='confirmation'):
        await activities.collect_edit_inputs(
            EditInput(upload_id='u1', user_id='user', collection_entry_id='c1')
        )


async def test_collect_refuses_without_a_sheet_or_with_a_pending_transcript(upload):
    data = EditInput(upload_id='u1', user_id='user', collection_entry_id='c1')
    upload.inputs[1].text = None
    with pytest.raises(ApplicationError, match='not transcribed'):
        await activities.collect_edit_inputs(data)
    del upload.writer.files[ORIGINAL_FILE]
    with pytest.raises(ApplicationError, match='no sheet was uploaded'):
        await activities.collect_edit_inputs(data)


# --- one input, first time -----------------------------------------------------


async def test_prepare_gives_the_two_prompts_for_the_original_sheet(
    upload, synthetic_xlsx
):
    prepared = await activities.prepare_input(prepare(upload, 1))
    assert prepared['cached'] is False
    a, b = prepared['prompts']
    assert a['system'] == system_prompt(second_opinion=False)
    assert b['system'] == system_prompt(second_opinion=True)
    assert a['text'].endswith(f'\nInstruction: {SAID}')
    assert a['schema'] == b['schema'] == proposed_edits_schema(build(synthetic_xlsx))
    assert upload.writer.written == []


async def test_apply_caches_the_agreed_proposal_and_reports_the_edit(upload):
    report = await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT), answer(SPEED_EDIT)])
    )
    assert report['input'] == 1
    assert report['entry_id'] == 'e1'
    assert report['text'] == SAID
    assert report['from_cache'] is False
    assert report['cells_written'] == 0
    assert report['refused'] is None
    assert report['confirmed'] == []
    assert (report['unresolved'], report['not_applied'], report['unchanged']) == (
        0,
        0,
        0,
    )
    (ready,) = report['ready']
    assert (
        ready['line']
        == '2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, samples 1–3'
    )
    assert report['preview'].startswith(ready['line'])
    assert ready['checks'] == []  # a clean edit: no check
    assert {k: ready[k] for k in ('step', 'column', 'old', 'new', 'samples')} == {
        'step': '2: Spin Coating',
        'column': 'Rotation speed [rpm]',
        'old': '3000',
        'new': '4000',
        'samples': 'samples 1–3',
    }

    assert upload.writer.written == [JOB_CACHE]  # the job's own, not the shared
    data = cache_file(upload)
    assert (data['model'], data['prompt_version']) == (MODEL, CACHE_VERSION)
    (entry,) = data['proposals'].values()
    assert entry['job'] == 'job-1'
    assert entry['proposal']['edits'] == [SPEED_EDIT]

    # the same words on the same sheet: nothing to ask
    prepared = await activities.prepare_input(prepare(upload, 1))
    assert prepared == {'cached': True, 'prompts': []}
    again = await activities.agree_cache_and_apply_input(apply(upload, 1, []))
    assert again == {**report, 'from_cache': True}
    assert upload.writer.written == [JOB_CACHE]  # not rewritten


async def test_the_report_counts_refused_and_unchanged_edits(upload):
    fast = {**SPEED_EDIT, 'new': 'fast'}  # a word in a number column: refused
    report = await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(fast), answer(fast)])
    )
    assert (report['ready'], report['not_applied'], report['unchanged']) == ([], 1, 0)
    same = {**SPEED_EDIT, 'new': '3000'}  # the cells hold 3000 already
    report = await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(same), answer(same)])
    )
    assert (report['ready'], report['not_applied'], report['unchanged']) == ([], 0, 1)


async def test_disagreeing_answers_make_the_input_uncertain(upload):
    other = {**SPEED_EDIT, 'target': 'C027'}
    report = await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT), answer(other)])
    )
    assert report['ready'] == []
    assert report['unresolved'] == 1  # the page says "Unclear"
    assert report['preview'].startswith(f'? uncertain: "{SAID}"')


async def test_an_answer_the_child_could_not_get_is_this_inputs_problem(upload):
    report = await activities.agree_cache_and_apply_input(
        apply(
            upload,
            1,
            [answer(SPEED_EDIT), LlmAnswer(error='Max retry attempts reached.')],
        )
    )
    assert report['ready'] == []
    assert report['preview'] == (
        '? sand could not propose an edit for this note. '
        'Try again, or write the note more clear.'
    )
    assert (await activities.prepare_input(prepare(upload, 1)))['cached'] is True
    # the next job asks again: an unreadable answer is never published
    await activities.agree_cache_and_apply_input(apply(upload, 2, [answer()] * 2))
    await activities.store_final_sheet_and_changelog(job(upload))
    (published,) = cache_file(upload, CACHE_FILE)['proposals'].values()
    assert published['proposal']['unresolved'] == []  # input 2 only
    assert (await activities.prepare_input(prepare(upload, 1, 'job-2')))[
        'cached'
    ] is False


async def test_the_old_value_is_checked_where_the_model_read_it(upload):
    misread = {**SPEED_EDIT, 'old': '2000'}
    await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2)
    )
    await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(misread)] * 2, 'job-2')
    )
    assert [e['old_read'] for e in cache_file(upload)['proposals'].values()] == [[[]]]
    [entry] = cache_file(upload, job_cache_file('job-2'))['proposals'].values()
    [[found]] = entry['old_read']
    assert "not '2000'" in found


async def test_applying_twice_with_the_same_answers_is_one_entry(upload):
    answers = [answer(SPEED_EDIT)] * 2
    first = await activities.agree_cache_and_apply_input(apply(upload, 1, answers))
    again = await activities.agree_cache_and_apply_input(apply(upload, 1, answers))
    assert again == first
    assert len(cache_file(upload)['proposals']) == 1


async def test_two_jobs_on_one_upload_cannot_lose_each_others_answers(upload):
    """Each running job writes its own file; the shared cache is only
    published at a job's end, and a job never reads it mid-run for
    what it got itself."""
    a, b = 'job-a', 'job-b'
    await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2, job_id=a)
    )
    await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2, job_id=b)
    )
    await activities.agree_cache_and_apply_input(
        apply(upload, 2, [answer()] * 2, job_id=b)
    )
    await activities.store_final_sheet_and_changelog(job(upload, job_id=b))
    assert job_cache_file(b) not in upload.writer.files
    assert len(cache_file(upload, CACHE_FILE)['proposals']) == 2  # noqa: PLR2004

    # job a goes on: its own answer for input 1 is still there, and what b
    # published serves it for input 2
    assert (await activities.prepare_input(prepare(upload, 2, job_id=a)))[
        'cached'
    ] is True
    report = await activities.agree_cache_and_apply_input(
        apply(upload, 2, [], job_id=a)
    )
    assert report['from_cache'] is True
    await activities.store_final_sheet_and_changelog(job(upload, job_id=a))
    assert job_cache_file(a) not in upload.writer.files
    shared = cache_file(upload, CACHE_FILE)['proposals']
    assert sorted(e['job'] for e in shared.values()) == [a, b]

    # the next job starts from everything both published
    assert (await activities.prepare_input(prepare(upload, 2, job_id='job-c')))[
        'cached'
    ] is True


def speed_edit(first, last, new):
    """Named by its old value: input 1 writing 4000 moves the 3000s."""
    said = f'speed {new} for samples {first} to {last} from 3000'
    edit = {
        **SPEED_EDIT,
        'rows': {'first': first, 'last': last, 'subbatch': None},
        'unit': None,
        'said_old': '3000',
        'new': new,
        'said': said,
    }
    return said, edit


async def test_a_rejection_reads_again_the_inputs_that_named_its_value(upload):
    said_2, edit_2 = speed_edit('4', '5', '5000')
    said_3, edit_3 = speed_edit('8', '9', '6000')
    upload.inputs = [
        collected('e1', SAID),
        collected('e2', said_2),
        collected('e3', said_3),
    ]
    # the first round: every input read once, with the edits above it
    first = [
        await activities.agree_cache_and_apply_input(apply(upload, n, [answer(e)] * 2))
        for n, e in ((1, SPEED_EDIT), (2, edit_2), (3, edit_3))
    ]
    await activities.store_final_sheet_and_changelog(job(upload))
    [key_1] = [edit['key'] for edit in first[0]['ready']]

    # input 1 confirmed: the inputs below were read on that sheet
    await confirm(upload, key_1)
    for number in (2, 3):
        prepared = await activities.prepare_input(prepare(upload, number, 'job-2'))
        assert prepared == {'cached': True, 'prompts': []}
    del upload.writer.files[CONFIRMATIONS_FILE]

    # input 1 rejected: the 3000 inputs 2 and 3 named now sits on samples
    # 1-3 too; each is asked again, whatever is undecided above it
    rejected = rejecting(job(upload, 'job-3'), [key_1])
    for number, edit in ((2, edit_2), (3, edit_3)):
        prepared = await activities.prepare_input(
            prepare(upload, number, snapshot=rejected)
        )
        assert len(prepared['prompts']) == 2  # noqa: PLR2004
        await activities.agree_cache_and_apply_input(
            apply(upload, number, [answer(edit)] * 2, snapshot=rejected)
        )
    await activities.store_final_sheet_and_changelog(rejected)

    # the next job replays all three: no input needs the model
    again = rejecting(job(upload, 'job-4'), [key_1])
    replayed = await activities.replay_cached_inputs(
        ReplayInput(**again.model_dump(), entry_ids=['e1', 'e2', 'e3'])
    )
    assert replayed['replayed'] == 3  # noqa: PLR2004
    assert replayed['reports'][0]['rejected'] == [key_1]


async def test_an_answer_that_still_holds_is_not_asked_again(upload):
    said = 'time 2 to 300 for samples 4 to 5'
    time_edit = {
        **SPEED_EDIT,
        'target': 'C014',
        'rows': {'first': '4', 'last': '5', 'subbatch': None},
        'old': '600',
        'new': '300',
        'said': said,
    }
    upload.inputs = [collected('e1', SAID), collected('e2', said)]
    first = await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2)
    )
    await activities.agree_cache_and_apply_input(
        apply(upload, 2, [answer(time_edit)] * 2)
    )
    await confirm(upload, first['ready'][0]['key'])

    assert await activities.prepare_input(prepare(upload, 2)) == {
        'cached': True,
        'prompts': [],
    }
    report = await activities.agree_cache_and_apply_input(apply(upload, 2, []))
    assert len(report['ready']) == 1
    assert len(cache_file(upload)['proposals']) == 2  # noqa: PLR2004


async def test_applying_without_answers_needs_the_cache(upload):
    with pytest.raises(ApplicationError, match='no answer and no cached proposal'):
        await activities.agree_cache_and_apply_input(apply(upload, 1, []))


async def test_a_lost_cache_under_a_running_job_stops_it(upload):
    await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2)
    )
    del upload.writer.files[JOB_CACHE]
    with pytest.raises(ApplicationError, match='has no cached proposal'):
        await activities.prepare_input(prepare(upload, 2))


# --- the replay ------------------------------------------------------------------


async def test_replay_applies_the_leading_cached_inputs_in_one_go(upload):
    assert await activities.replay_cached_inputs(replay(upload)) == {
        'replayed': 0,
        'reports': [],
    }
    first = await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2)
    )
    replayed = await activities.replay_cached_inputs(replay(upload))
    assert replayed == {'replayed': 1, 'reports': [{**first, 'from_cache': True}]}
    second = await activities.agree_cache_and_apply_input(
        apply(upload, 2, [answer()] * 2)
    )
    written = list(upload.writer.written)
    replayed = await activities.replay_cached_inputs(replay(upload))
    assert replayed['replayed'] == 2  # noqa: PLR2004
    assert replayed['reports'][1] == {**second, 'from_cache': True}
    assert upload.writer.written == written  # the replay writes nothing


async def test_replay_writes_the_confirmed_edits_and_stops_where_the_sheet_is_new(
    upload,
):
    first = await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2)
    )
    await activities.agree_cache_and_apply_input(apply(upload, 2, [answer()] * 2))
    [key] = [edit['key'] for edit in first['ready']]
    await confirm(upload, key)
    # input 2 was read with input 1's edit: both are replayed
    replayed = await activities.replay_cached_inputs(replay(upload))
    assert replayed['replayed'] == 2  # noqa: PLR2004
    report = replayed['reports'][0]
    assert report['confirmed'] == [key]
    assert report['cells_written'] == SAMPLES_1_TO_3
    # input 1 rejected instead: input 2 is read again
    del upload.writer.files[CONFIRMATIONS_FILE]
    rejected = rejecting(job(upload), [key])
    replayed = await activities.replay_cached_inputs(
        ReplayInput(**rejected.model_dump(), entry_ids=['e1', 'e2'])
    )
    assert replayed['replayed'] == 1


async def test_replay_stops_at_a_lost_cache(upload):
    await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2)
    )
    del upload.writer.files[JOB_CACHE]
    assert (await activities.replay_cached_inputs(replay(upload)))['replayed'] == 0


# --- the chain of inputs and the confirmations ---------------------------------


CONFIRMED = {'by': 'tester', 'at': '2026-10-28T12:16:05+00:00', 'input': 'n1'}


async def confirm(upload, key: str) -> None:
    upload.writer.files[CONFIRMATIONS_FILE] = json.dumps({key: CONFIRMED}).encode()


async def test_a_confirmed_edit_is_written_and_the_next_input_sees_the_same_sheet(
    upload,
):
    report = await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2)
    )
    (ready,) = report['ready']
    before = await activities.prepare_input(prepare(upload, 2))
    assert '"4000"' in before['prompts'][0]['text']  # the edit waiting above

    await confirm(upload, ready['key'])
    written = await activities.agree_cache_and_apply_input(apply(upload, 1, []))
    assert written['confirmed'] == [ready['key']]
    assert written['held'] == []
    assert written['cells_written'] == SAMPLES_1_TO_3

    after = await activities.prepare_input(prepare(upload, 2))
    assert before == after


async def test_a_confirmation_made_during_the_job_waits_for_the_next_run(upload):
    """The job's confirmations are the ones collected at its start: one
    landing while the model answers is written by the next run."""
    collected = job(upload)
    report = await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2)
    )
    (ready,) = report['ready']
    prepared = await activities.prepare_input(prepare(upload, 2, snapshot=collected))
    await confirm(upload, ready['key'])  # lands while the model answers
    second = await activities.agree_cache_and_apply_input(
        apply(upload, 2, [answer()] * 2, snapshot=collected)
    )
    assert second['cells_written'] == 0
    assert (await activities.prepare_input(prepare(upload, 2, snapshot=collected))) == {
        'cached': True,
        'prompts': [],
    }
    assert prepared['cached'] is False
    assert (await activities.store_final_sheet_and_changelog(collected))[
        'changed'
    ] is False

    # the next job, collected after the confirmation, writes it; input 2
    # was read on that sheet
    assert (await activities.agree_cache_and_apply_input(apply(upload, 1, [])))[
        'cells_written'
    ] == 3  # noqa: PLR2004
    assert (await activities.prepare_input(prepare(upload, 2)))['cached'] is True


async def test_a_writer_refusal_is_reported_not_raised(upload, monkeypatch):
    from sand.sheet_edit import apply as pipeline
    from sand.sheet_edit.write import WriteRefused

    report = await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2)
    )
    await confirm(upload, report['ready'][0]['key'])

    def refuse(state, writes):
        raise WriteRefused('the cell is locked')

    monkeypatch.setattr(pipeline, 'write_cells', refuse)
    refused = await activities.agree_cache_and_apply_input(apply(upload, 1, []))
    assert refused['refused'] == 'the cell is locked'
    assert refused['cells_written'] == 0


# --- the end of the job -----------------------------------------------------------


async def test_store_final_sheet_and_changelog_stores_the_final_sheet_with_its_changelog(
    upload, synthetic_xlsx
):
    report = await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2)
    )
    await confirm(upload, report['ready'][0]['key'])
    clear = {
        'target': 'C013',
        'rows': {'samples': ['1'], 'subbatch': None},
        'anchor': None,
        'old': 'IPA',
        'unit': None,
        'said_old': None,
        'new': '',
        'said': 'clear solvent 2 of sample 1',
    }
    await activities.agree_cache_and_apply_input(apply(upload, 2, [answer(clear)] * 2))

    done = await activities.store_final_sheet_and_changelog(job(upload))
    final = upload.writer.files[FINAL_FILE]
    assert done['final'] == sha256(final)
    # the log beside the sheet is the one the status renders
    stored = changelog.from_json(upload.writer.files[CHANGELOG_FILE].decode())
    assert stored.final == sha256(final)
    assert changelog.render(stored) == done['changelog']
    assert done['changed'] is True
    assert done['changes'] == SAMPLES_1_TO_3  # input 2 is not confirmed
    assert done['stale_confirmations'] == 0
    assert 'confirmed by tester, 2026-10-28 12:16 UTC' in done['changelog']
    # what the sheet holds now, for the table: the confirmed edit only
    ready = report['ready'][0]
    assert done['written'] == [
        {
            'step': ready['step'],
            'column': ready['column'],
            'value': '4000',
            'samples': 'samples 1–3',
        }
    ]
    assert f'original  sha256 {sha256(synthetic_xlsx)}' in done['changelog']
    assert final != synthetic_xlsx


async def answer_both(upload):
    await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2)
    )
    await activities.agree_cache_and_apply_input(apply(upload, 2, [answer()] * 2))


async def test_the_end_of_a_job_is_one_request(upload):
    """Every write is a NOMAD run: the sheet, the changelog, the shared
    cache and the completed status go together."""
    await answer_both(upload)
    before = upload.writer.requests
    status = {'job_id': 'job-1', 'inputs': [{'input': 1}], 'model': MODEL}

    done = await activities.store_final_sheet_and_changelog(
        StoreInput(**job(upload).model_dump(exclude={'status'}), status=status)
    )

    # one PUT, then the job's own cache file goes
    assert upload.writer.requests - before == 1 + 1
    assert JOB_CACHE not in upload.writer.files
    stored = json.loads(upload.writer.files[STATUS_FILE])
    assert (stored['phase'], stored['final']) == ('completed', done['final'])
    assert stored['inputs'] == [{'input': 1}]
    for name in (FINAL_FILE, CHANGELOG_FILE, CACHE_FILE):
        assert name in upload.writer.files


async def test_an_input_read_is_one_request_with_its_status(upload):
    status = {'job_id': 'job-1', 'phase': 'interpreting', 'inputs': []}
    before = upload.writer.requests

    report = await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2).model_copy(update={'status': status})
    )

    assert upload.writer.requests - before == 1
    stored = json.loads(upload.writer.files[STATUS_FILE])
    assert stored['inputs'] == [report]
    assert job_cache_file('job-1') in upload.writer.files


async def test_an_input_whose_write_was_lost_fails_for_a_retry(upload, monkeypatch):
    """The PUT only queues NOMAD's file update: a lost one is not a read
    note (Temporal retries the apply, with the answers it has)."""

    async def lost(client, upload_id, files, directory=''):
        upload.writer.requests += 1

    monkeypatch.setattr(upload.writer, 'upload_raw_files', lost)
    status = {'job_id': 'job-1', 'phase': 'interpreting', 'inputs': []}
    with pytest.raises(ApplicationError, match='did not store'):
        await activities.agree_cache_and_apply_input(
            apply(upload, 1, [answer(SPEED_EDIT)] * 2).model_copy(
                update={'status': status}
            )
        )


async def test_a_replay_of_every_input_stores_the_sheet_in_one_request(upload):
    """Confirm all: nothing to ask, so the sheet built for the reports is
    the final one, stored with the completed status at once."""
    await answer_both(upload)
    await activities.store_final_sheet_and_changelog(job(upload))
    # the API's reservation, made before the job started
    reserved = {'job_id': 'starting', 'phase': 'starting', 'updated_at': STARTED}
    upload.writer.files[STATUS_FILE] = json.dumps(reserved).encode()
    before = upload.writer.requests
    status = {'job_id': 'job-2', 'model': MODEL, 'started_at': STARTED}

    result = await activities.replay_cached_inputs(
        replay(upload, job_id='job-2').model_copy(update={'store_status': status})
    )

    assert upload.writer.requests - before == 1
    stored = json.loads(upload.writer.files[STATUS_FILE])
    assert stored['phase'] == 'completed'
    assert stored['final'] == result['done']['final']
    assert [r['input'] for r in stored['inputs']] == [1, 2]


async def test_a_replay_whose_end_was_lost_fails_for_a_retry(upload, monkeypatch):
    """The job's own reservation still there after the PUT: nothing was
    stored, not a newer job's."""
    await answer_both(upload)
    await activities.store_final_sheet_and_changelog(job(upload))
    reserved = {'job_id': 'starting', 'phase': 'starting', 'updated_at': STARTED}
    upload.writer.files[STATUS_FILE] = json.dumps(reserved).encode()

    async def lost(client, upload_id, files, directory=''):
        upload.writer.requests += 1

    monkeypatch.setattr(upload.writer, 'upload_raw_files', lost)
    status = {'job_id': 'job-2', 'model': MODEL, 'started_at': STARTED}
    with pytest.raises(ApplicationError, match='completed status'):
        await activities.replay_cached_inputs(
            replay(upload, job_id='job-2').model_copy(update={'store_status': status})
        )


async def test_a_replay_after_a_read_input_reports_only_the_ones_after(upload):
    """The job read input 1; input 2's answer holds: the replay after it
    reports input 2 and stores the sheet, with input 1's report as read
    and this job's answers published."""
    await answer_both(upload)
    await activities.store_final_sheet_and_changelog(job(upload))
    reserved = {'job_id': 'starting', 'phase': 'starting', 'updated_at': STARTED}
    upload.writer.files[STATUS_FILE] = json.dumps(reserved).encode()
    # input 1 read again by the next job (as after a change of it)
    first = await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2, job_id='job-2')
    )
    status = {'job_id': 'job-2', 'started_at': STARTED, 'inputs': [first]}
    before = upload.writer.requests

    result = await activities.replay_cached_inputs(
        replay(upload, job_id='job-2').model_copy(
            update={'store_status': status, 'after': 1}
        )
    )

    assert result['replayed'] == 2  # noqa: PLR2004
    assert [r['input'] for r in result['reports']] == [2]
    assert upload.writer.requests - before == 2  # noqa: PLR2004  the store, the delete
    stored = json.loads(upload.writer.files[STATUS_FILE])
    assert stored['phase'] == 'completed'
    assert stored['inputs'] == [first, result['reports'][0]]
    assert job_cache_file('job-2') not in upload.writer.files


async def test_a_replay_with_an_input_to_read_stores_nothing(upload):
    await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2)
    )
    before = upload.writer.requests

    result = await activities.replay_cached_inputs(
        replay(upload).model_copy(update={'store_status': {'job_id': 'job-1'}})
    )

    assert (result['replayed'], 'done' in result) == (1, False)
    assert upload.writer.requests == before


async def test_a_job_that_asked_nothing_deletes_nothing(upload):
    # Confirm all: every answer cached, no file of its own
    await answer_both(upload)
    await activities.store_final_sheet_and_changelog(job(upload))
    before = upload.writer.requests

    await activities.store_final_sheet_and_changelog(job(upload, job_id='job-2'))

    assert upload.writer.requests - before == 1


def ending(upload, job_id='job-1') -> StoreInput:
    status = {'job_id': job_id, 'started_at': STARTED, 'model': MODEL, 'inputs': []}
    return StoreInput(
        **job(upload, job_id).model_dump(exclude={'status'}), status=status
    )


async def test_the_end_is_checked_once_nomad_stored_it(upload, monkeypatch):
    """The PUT only queues NOMAD's file update: a lost one is not success
    (Temporal retries the activity)."""
    await answer_both(upload)

    async def lost(client, upload_id, files, directory=''):
        upload.writer.requests += 1

    monkeypatch.setattr(upload.writer, 'upload_raw_files', lost)
    with pytest.raises(ApplicationError, match='did not store'):
        await activities.store_final_sheet_and_changelog(ending(upload))


async def test_a_retry_after_a_newer_job_took_over_writes_nothing(upload):
    await answer_both(upload)
    newer = {
        'job_id': 'starting',
        'phase': 'starting',
        'updated_at': '2026-11-18T10:05:00+00:00',
    }
    upload.writer.files[STATUS_FILE] = json.dumps(newer).encode()
    before = upload.writer.requests

    await activities.store_final_sheet_and_changelog(ending(upload))

    assert upload.writer.requests == before
    assert json.loads(upload.writer.files[STATUS_FILE]) == newer


async def test_its_own_reservation_is_replaced(upload):
    # a job that asked nothing writes no status before its end
    await answer_both(upload)
    own = {
        'job_id': 'starting',
        'phase': 'starting',
        'updated_at': '2026-11-18T09:59:59+00:00',
    }
    upload.writer.files[STATUS_FILE] = json.dumps(own).encode()

    await activities.store_final_sheet_and_changelog(ending(upload))

    assert json.loads(upload.writer.files[STATUS_FILE])['phase'] == 'completed'


async def test_a_cleanup_that_fails_does_not_fail_the_end(upload, monkeypatch):
    await answer_both(upload)

    async def down(client, upload_id, name):
        raise RuntimeError('NOMAD down')

    monkeypatch.setattr(upload.writer, 'delete_raw_file', down)
    done = await activities.store_final_sheet_and_changelog(ending(upload))

    stored = json.loads(upload.writer.files[STATUS_FILE])
    assert (stored['phase'], stored['final']) == ('completed', done['final'])


async def test_a_failure_does_not_replace_a_newer_jobs_status(upload):
    newer = {'job_id': 'job-2', 'phase': 'interpreting', 'updated_at': STARTED}
    upload.writer.files[STATUS_FILE] = json.dumps(newer).encode()

    await activities.write_edit_status(
        WriteStatusInput(
            upload_id='u1',
            user_id='user',
            status={'job_id': 'job-1', 'phase': 'failed', 'started_at': STARTED},
        )
    )

    assert json.loads(upload.writer.files[STATUS_FILE]) == newer


async def test_store_final_sheet_and_changelog_without_confirmations_stores_the_original(
    upload, synthetic_xlsx
):
    await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2)
    )
    await activities.agree_cache_and_apply_input(apply(upload, 2, [answer()] * 2))
    done = await activities.store_final_sheet_and_changelog(job(upload))
    assert done['changed'] is False
    assert done['changes'] == 0
    assert upload.writer.files[FINAL_FILE] == synthetic_xlsx


async def test_a_confirmation_matching_nothing_is_counted(upload):
    await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2)
    )
    await activities.agree_cache_and_apply_input(apply(upload, 2, [answer()] * 2))
    await confirm(upload, 'a key from a reworded input')
    done = await activities.store_final_sheet_and_changelog(job(upload))
    assert done['changed'] is False
    assert done['stale_confirmations'] == 1


async def test_a_failed_check_stores_nothing(upload, monkeypatch):
    from sand.sheet_edit import changelog

    await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2)
    )
    await activities.agree_cache_and_apply_input(apply(upload, 2, [answer()] * 2))

    def fail(original, final, log):
        raise changelog.CheckFailed('a cell differs')

    monkeypatch.setattr(changelog, 'check', fail)
    with pytest.raises(ApplicationError, match='does not match its changelog'):
        await activities.store_final_sheet_and_changelog(job(upload))
    assert FINAL_FILE not in upload.writer.files


async def test_status_is_written_where_the_gui_polls(upload):
    await activities.write_edit_status(
        WriteStatusInput(upload_id='u1', user_id='user', status={'phase': 'collecting'})
    )
    assert json.loads(upload.writer.files[STATUS_FILE]) == {'phase': 'collecting'}
