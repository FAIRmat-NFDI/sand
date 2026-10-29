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
    WriteStatusInput,
)
from sand.services.voice_eln import CollectedInput
from sand.sheet_edit.catalog import build
from sand.sheet_edit.edits import proposed_edits_schema
from sand.sheet_edit.resolve import PROMPT_VERSION, system_prompt
from sand.sheet_edit.session import (
    CACHE_FILE,
    CONFIRMATIONS_FILE,
    FINAL_FILE,
    ORIGINAL_FILE,
    STATUS_FILE,
    job_cache_file,
)
from sand.sheet_edit.workbook import sha256

pytestmark = pytest.mark.asyncio

MODEL = 'gemini/test'
SPEED = 'C025'
SAID = 'speed 4000 for samples 1 to 3'
SAMPLES_1_TO_3 = 3  # cells one confirmed edit of SAID writes
SPEED_EDIT = {
    'target': SPEED,
    'rows': {'first': '1', 'last': '3', 'subbatch': None},
    'anchor': None,
    'old': '3000',
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

    async def read_raw_file(self, client, upload_id, name):
        return self.files.get(name)

    async def read_json(self, client, upload_id, name):
        raw = self.files.get(name)
        return None if raw is None else json.loads(raw)

    async def upload_raw_file(self, client, upload_id, name, content, content_type):
        self.files[name] = content
        self.written.append(name)

    async def write_json(self, client, upload_id, name, payload):
        await self.upload_raw_file(
            client, upload_id, name, json.dumps(payload).encode(), 'application/json'
        )

    async def delete_raw_file(self, client, upload_id, name):
        self.files.pop(name, None)


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


def job(upload, job_id='job-1') -> JobInput:
    """As the workflow builds it from `collect_edit_inputs`: the
    confirmations file as it is now."""
    raw = upload.writer.files.get(CONFIRMATIONS_FILE)
    return JobInput(
        upload_id='u1',
        user_id='user',
        job_id=job_id,
        llm_model_name=MODEL,
        texts=[i.text for i in upload.inputs],
        confirmations={} if raw is None else json.loads(raw),
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
    (ready,) = report['ready']
    assert (
        ready['line']
        == '2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, samples 1–3'
    )
    assert report['preview'].startswith(ready['line'])

    assert upload.writer.written == [JOB_CACHE]  # the job's own, not the shared
    data = cache_file(upload)
    assert (data['model'], data['prompt_version']) == (MODEL, PROMPT_VERSION)
    (entry,) = data['proposals'].values()
    assert entry['job'] == 'job-1'
    assert entry['proposal']['edits'] == [SPEED_EDIT]

    # the same words on the same sheet: nothing to ask
    prepared = await activities.prepare_input(prepare(upload, 1))
    assert prepared == {'cached': True, 'prompts': []}
    again = await activities.agree_cache_and_apply_input(apply(upload, 1, []))
    assert again == {**report, 'from_cache': True}
    assert upload.writer.written == [JOB_CACHE]  # not rewritten


async def test_disagreeing_answers_make_the_input_uncertain(upload):
    other = {**SPEED_EDIT, 'target': 'C027'}
    report = await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT), answer(other)])
    )
    assert report['ready'] == []
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
        "? not done, the model's answer could not be read: Max retry attempts reached."
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


# --- the chain of inputs and the confirmations ---------------------------------


CONFIRMED = {'by': 'tester', 'at': '2026-10-28T12:16:05+00:00'}


async def confirm(upload, key: str) -> None:
    upload.writer.files[CONFIRMATIONS_FILE] = json.dumps({key: CONFIRMED}).encode()


async def test_a_confirmed_edit_is_written_and_the_next_input_sees_the_new_sheet(
    upload, synthetic_xlsx
):
    report = await activities.agree_cache_and_apply_input(
        apply(upload, 1, [answer(SPEED_EDIT)] * 2)
    )
    (ready,) = report['ready']
    before = await activities.prepare_input(prepare(upload, 2))

    await confirm(upload, ready['key'])
    written = await activities.agree_cache_and_apply_input(apply(upload, 1, []))
    assert written['confirmed'] == [ready['key']]
    assert written['cells_written'] == SAMPLES_1_TO_3

    after = await activities.prepare_input(prepare(upload, 2))
    assert after['cached'] is False
    assert before['prompts'][0]['text'] != after['prompts'][0]['text']
    assert '"4000"' in after['prompts'][0]['text']


async def test_a_confirmation_made_during_the_job_waits_for_the_next_run(upload):
    """The job's confirmations are the ones collected at its start: the
    answers of input 2 were asked for the sheet without the edit of input
    1, so they are cached and applied for that sheet."""
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

    # the next job, collected after the confirmation, writes it
    assert (await activities.agree_cache_and_apply_input(apply(upload, 1, [])))[
        'cells_written'
    ] == 3  # noqa: PLR2004
    assert (await activities.prepare_input(prepare(upload, 2)))['cached'] is False


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
        'new': '',
        'said': 'clear solvent 2 of sample 1',
    }
    await activities.agree_cache_and_apply_input(apply(upload, 2, [answer(clear)] * 2))

    done = await activities.store_final_sheet_and_changelog(job(upload))
    final = upload.writer.files[FINAL_FILE]
    assert done['final'] == sha256(final)
    assert done['changed'] is True
    assert done['changes'] == SAMPLES_1_TO_3  # input 2 is not confirmed
    assert done['stale_confirmations'] == 0
    assert 'confirmed by tester, 2026-10-28 12:16 UTC' in done['changelog']
    assert f'original  sha256 {sha256(synthetic_xlsx)}' in done['changelog']
    assert final != synthetic_xlsx


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
