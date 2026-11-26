"""EditSheetWorkflow on Temporal's test server: the activities are stubs
that record what they got, the LLM child echoes its payload back."""

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from sand.actions.common import LLM_TASK_QUEUE, error_message
from sand.actions.edit.models import (
    ApplyInput,
    EditInput,
    PrepareInput,
    ReplayInput,
    StoreInput,
    WriteStatusInput,
)
from sand.actions.edit.workflows import TEMPERATURE, EditSheetWorkflow
from sand.sheet_edit.resolve import PROMPT_VERSION

pytestmark = pytest.mark.asyncio

MODEL = 'gemini/test'
PROMPTS = [
    {'system': 'rules A', 'text': 'table\nInstruction: speed 4000', 'schema': {'a': 1}},
    {'system': 'rules B', 'text': 'elbat\nInstruction: speed 4000', 'schema': {'a': 1}},
]
EDIT = EditInput(upload_id='u1', user_id='user', collection_entry_id='c1')


@workflow.defn(name='ExtractionWorkflow')
class FakeExtraction:
    """nomad-llm-extraction's child, as the workflow sees it: the parsed
    answer, or an error message after its retries."""

    @workflow.run
    async def run(self, payload: dict) -> dict:
        if 'FAIL' in payload['prompt']:
            return {'extracted_data': {}, 'err_message': 'Max retry attempts reached.'}
        if 'RAISE' in payload['prompt']:
            raise ApplicationError('provider down', non_retryable=True)
        if 'SLOW' in payload['prompt']:
            await workflow.sleep(timedelta(minutes=6))  # skipped in tests
        return {'extracted_data': {'got': payload}, 'err_message': None}


@dataclass
class Stubs:
    """The activities, recording every call; `cached` says which inputs
    the model already answered: the leading ones are replayed, a later
    one makes prepare report nothing to ask."""

    texts: list[str]
    cached: set[int] = field(default_factory=set)
    failing: str | None = None  # the activity that raises
    prompts_for: dict[int, int] = field(default_factory=dict)  # input -> how many
    calls: list[tuple[str, object]] = field(default_factory=list)
    read: set[int] = field(default_factory=set)  # applied in this job

    def activities(self):
        stubs = self

        @activity.defn(name='collect_edit_inputs')
        async def collect(data: EditInput) -> dict:
            stubs.record('collect', data)
            return {
                'texts': stubs.texts,
                'input_entry_ids': [f'e{i + 1}' for i in range(len(stubs.texts))],
                'labels': ['' for _ in stubs.texts],
                'confirmations': {
                    'k1': {
                        'by': 'tester',
                        'at': '2026-10-28T12:16:05+00:00',
                        'input': 'n1',
                    }
                },
                'rejections': [],
                'llm_model_name': MODEL,
            }

        @activity.defn(name='replay_cached_inputs')
        async def replay(data: ReplayInput) -> dict:
            stubs.record('replay', data)
            replayed = 0
            # an input read in this job is cached from then on
            while replayed + 1 in stubs.cached | stubs.read:
                replayed += 1
            reports = [
                {'input': k, 'preview': f'preview {k}', 'from_cache': True}
                for k in range(data.after + 1, replayed + 1)
            ]
            result = {'replayed': replayed, 'reports': reports}
            if data.store_status and replayed == len(data.texts):
                # the real one stores the sheet and the completed status
                result['done'] = {'final': 'abc', 'changed': True, 'changes': 3}
                earlier = data.store_status.get('inputs', [])[: data.after]
                stubs.record(
                    'status',
                    {
                        **data.store_status,
                        'inputs': [*earlier, *reports],
                        'phase': 'completed',
                    },
                )
            return result

        @activity.defn(name='prepare_input')
        async def prepare(data: PrepareInput) -> dict:
            stubs.record('prepare', data)
            if data.number in stubs.cached:
                return {'cached': True, 'prompts': []}
            text = data.texts[data.number - 1]
            how_many = stubs.prompts_for.get(data.number, len(PROMPTS))
            return {
                'cached': False,
                'prompts': [
                    {**p, 'text': f'{p["text"]} {text}'} for p in PROMPTS[:how_many]
                ],
            }

        @activity.defn(name='agree_cache_and_apply_input')
        async def apply(data: ApplyInput) -> dict:
            stubs.record('apply', data)
            stubs.read.add(data.number)
            report = {'input': data.number, 'preview': f'preview {data.number}'}
            if data.status:
                # the real one writes the status with this report
                inputs = [*data.status['inputs'], report]
                stubs.record('status', {**data.status, 'inputs': inputs})
            return report

        @activity.defn(name='store_final_sheet_and_changelog')
        async def store(data: StoreInput) -> dict:
            stubs.record('store_final_sheet_and_changelog', data)
            done = {'final': 'abc', 'changed': True, 'changes': 3, 'changelog': 'log'}
            # the real one writes the completed status with the sheet
            stubs.record('status', {**data.status, 'phase': 'completed', **done})
            return done

        @activity.defn(name='write_edit_status')
        async def status(data: WriteStatusInput) -> None:
            stubs.record('status', data.status)

        return [collect, replay, prepare, apply, store, status]

    def record(self, name: str, data: object) -> None:
        if name == self.failing:
            raise ApplicationError(f'{name} broke', non_retryable=True)
        self.calls.append((name, data))

    def of(self, name: str) -> list:
        return [data for called, data in self.calls if called == name]


async def run(stubs: Stubs) -> dict:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        queue = f'sand-{uuid.uuid4()}'
        async with (
            Worker(
                env.client,
                task_queue=queue,
                workflows=[EditSheetWorkflow],
                activities=stubs.activities(),
            ),
            Worker(env.client, task_queue=LLM_TASK_QUEUE, workflows=[FakeExtraction]),
        ):
            # a workflow task that keeps failing would retry forever
            return await asyncio.wait_for(
                env.client.execute_workflow(
                    EditSheetWorkflow.run,
                    EDIT,
                    id=f'job-{uuid.uuid4()}',
                    task_queue=queue,
                ),
                timeout=120,
            )


async def test_each_input_in_order_two_opinions_each_then_the_sheet():
    stubs = Stubs(['speed 4000', 'clear solvent 2'])
    result = await run(stubs)

    # no "collecting" or "replaying" status: the reservation stands; each
    # input's status goes with its apply (one write), and one before the
    # model is asked, for the page's spinner
    assert [name for name, _ in stubs.calls] == [
        'collect',
        'replay',
        'status',
        'prepare',
        'status',
        'apply',
        'status',
        # what follows input 1 is not cached: nothing replayed
        'replay',
        'prepare',
        'status',
        'apply',
        'status',
        'store_final_sheet_and_changelog',
        'status',
    ]
    first, second = stubs.of('apply')
    assert [(f.number, f.entry_id) for f in (first, second)] == [(1, 'e1'), (2, 'e2')]
    assert first.texts == second.texts == ['speed 4000', 'clear solvent 2']
    assert first.llm_model_name == MODEL
    assert first.job_id == result['job_id']
    assert first.confirmations == {
        'k1': {'by': 'tester', 'at': '2026-10-28T12:16:05+00:00', 'input': 'n1'}
    }
    replay, after_first = stubs.of('replay')
    assert (replay.texts, replay.entry_ids) == (first.texts, ['e1', 'e2'])
    assert (replay.after, after_first.after) == (0, 1)
    assert replay.confirmations == first.confirmations
    (store,) = stubs.of('store_final_sheet_and_changelog')
    assert store.confirmations == first.confirmations
    # its status: the job's, with every report; no write of its own after
    assert store.status['job_id'] == result['job_id']
    assert [r['input'] for r in store.status['inputs']] == [1, 2]

    # the child got the prompt as composed, the model and temperature 0
    a, b = [json.loads(answer.text)['got'] for answer in first.answers]
    assert a['prompt'] == 'rules A\n\ntable\nInstruction: speed 4000 speed 4000'
    assert b['prompt'] == 'rules B\n\nelbat\nInstruction: speed 4000 speed 4000'
    assert a['extraction_schema'] == {'a': 1}
    assert a['llm_engine_config'] == {'model_name': MODEL}
    assert a['llm_engine_optional_params'] == {'temperature': TEMPERATURE}
    assert TEMPERATURE == 0
    assert [answer.error for answer in first.answers] == [None, None]

    assert result['inputs'] == [
        {'input': 1, 'preview': 'preview 1'},
        {'input': 2, 'preview': 'preview 2'},
    ]
    assert result['changelog'] == 'log'


async def test_status_shows_progress_then_the_result():
    stubs = Stubs(['speed 4000', 'clear solvent 2'])
    await run(stubs)
    statuses = stubs.of('status')
    # "writing-sheet" only as a heartbeat: the last status is fresh
    assert [s['phase'] for s in statuses] == [*['interpreting'] * 5, 'completed']
    # before the first input, then before each model call and after each
    assert [s.get('inputs_done') for s in statuses[:5]] == [0, 0, 1, 1, 2]
    assert [s.get('asking') for s in statuses] == [None, 'e1', None, 'e2', None, None]
    assert all(s['inputs_total'] == 2 for s in statuses[:5])  # noqa: PLR2004
    assert all(s['inputs_replayed'] == 0 for s in statuses[:5])
    assert [len(s['inputs']) for s in statuses] == [0, 0, 1, 1, 2, 2]
    done = statuses[-1]
    assert done['collection_entry_id'] == 'c1'
    assert done['model'] == MODEL
    assert done['prompt_version'] == PROMPT_VERSION
    assert done['temperature'] == 0
    assert (done['final'], done['changed'], done['changes']) == ('abc', True, 3)
    assert done['changelog'] == 'log'
    assert done['job_id'] == statuses[0]['job_id']


async def test_a_slow_model_adds_no_heartbeat():
    """Each input's own writes keep the status fresh: the heartbeat is
    for a slow prepare (its retries), not for the model."""
    stubs = Stubs(['SLOW speed 4000', 'SLOW clear solvent 2'])
    await run(stubs)
    phases = [
        (s['phase'], s.get('inputs_done'), s.get('asking')) for s in stubs.of('status')
    ]
    assert phases == [
        ('interpreting', 0, None),
        ('interpreting', 0, 'e1'),
        ('interpreting', 1, None),
        ('interpreting', 1, 'e2'),
        ('interpreting', 2, None),
        ('completed', None, None),
    ]


async def test_the_leading_cached_inputs_are_replayed_in_one_go():
    stubs = Stubs(['speed 4000', 'clear solvent 2', 'speed 3000'], cached={1, 2})
    result = await run(stubs)
    # one activity for inputs 1 and 2, then input 3 as usual
    assert [n for n, _ in stubs.calls if n != 'status'] == [
        'collect',
        'replay',
        'prepare',
        'apply',
        'store_final_sheet_and_changelog',
    ]
    (third,) = stubs.of('apply')
    assert (third.number, len(third.answers)) == (3, 2)
    assert [r['input'] for r in result['inputs']] == [1, 2, 3]
    assert [r['from_cache'] for r in result['inputs'][:2]] == [True, True]
    interpreting = [s for s in stubs.of('status') if s['phase'] == 'interpreting']
    # the replayed inputs 1 and 2 get no "asking"; input 3 does
    assert [s['inputs_done'] for s in interpreting] == [2, 2, 3]
    assert [s.get('asking') for s in interpreting] == [None, 'e3', None]
    assert all(s['inputs_replayed'] == 2 for s in interpreting)  # noqa: PLR2004
    assert [len(s['inputs']) for s in interpreting] == [2, 2, 3]


async def test_everything_cached_is_replayed_then_written():
    stubs = Stubs(['speed 4000', 'clear solvent 2'], cached={1, 2})
    result = await run(stubs)
    assert stubs.of('prepare') == stubs.of('apply') == []
    assert [r['input'] for r in result['inputs']] == [1, 2]
    # nothing to ask (Confirm all): the replay stores the sheet with the
    # completed status, one step, no other status written
    assert stubs.of('store_final_sheet_and_changelog') == []
    (done,) = stubs.of('status')
    assert done['phase'] == 'completed'
    assert [r['input'] for r in done['inputs']] == [1, 2]
    assert result['final'] == 'abc'


async def test_the_cached_inputs_after_a_read_one_are_one_step():
    """Notes 2 and 5 changed: 1 is replayed, 2 read, 3 and 4 replayed
    together, 5 read, then the sheet stored."""
    texts = ['speed 4000', 'anneal 120', 'time 30', 'speed +500', 'DMF']
    stubs = Stubs(texts, cached={1, 3, 4})
    result = await run(stubs)

    assert [a.number for a in stubs.of('apply')] == [2, 5]
    assert all(len(a.answers) == 2 for a in stubs.of('apply'))  # noqa: PLR2004
    assert [r.after for r in stubs.of('replay')] == [0, 2]
    assert [p.number for p in stubs.of('prepare')] == [2, 5]
    assert [r['input'] for r in result['inputs']] == [1, 2, 3, 4, 5]


async def test_a_replay_after_the_last_read_stores_the_sheet():
    stubs = Stubs(['speed 4000', 'clear solvent 2'], cached={2})
    result = await run(stubs)

    assert [a.number for a in stubs.of('apply')] == [1]
    assert stubs.of('store_final_sheet_and_changelog') == []
    completed = stubs.of('status')[-1]
    assert completed['phase'] == 'completed'
    assert [r['input'] for r in completed['inputs']] == [1, 2]
    assert result['final'] == 'abc'


async def test_a_replay_that_does_not_add_up_fails_the_job():
    stubs = Stubs(['speed 4000'], cached={1, 2})  # 2 replayed of 1
    with pytest.raises(WorkflowFailureError) as failure:
        await run(stubs)
    assert error_message(failure.value) == 'replayed 2 of 1 inputs with 2 reports'


async def test_an_answer_the_child_cannot_get_reaches_apply_as_an_error():
    stubs = Stubs(['FAIL this one', 'speed 4000'])
    await run(stubs)
    first, second = stubs.of('apply')
    assert [a.text for a in first.answers] == [None, None]
    assert [a.error for a in first.answers] == ['Max retry attempts reached.'] * 2
    assert [a.error for a in second.answers] == [None, None]


async def test_a_child_that_fails_outright_fails_the_job():
    stubs = Stubs(['speed 4000', 'RAISE here'])
    with pytest.raises(WorkflowFailureError) as failure:
        await run(stubs)
    assert error_message(failure.value) == 'provider down'
    last = stubs.of('status')[-1]
    assert (last['phase'], last['error']) == ('failed', 'provider down')
    assert [r['input'] for r in last['inputs']] == [1]  # what got through
    assert len(stubs.of('apply')) == 1


async def test_prepare_must_give_two_prompts_or_none():
    stubs = Stubs(['speed 4000'], prompts_for={1: 1})
    with pytest.raises(WorkflowFailureError) as failure:
        await run(stubs)
    assert error_message(failure.value) == 'input 1: 1 prompts, not 2'
    assert stubs.of('apply') == []


async def test_no_inputs_is_a_finished_job_with_nothing_to_do():
    stubs = Stubs([])
    result = await run(stubs)
    # the replay of no inputs stores the sheet: nothing is asked
    assert stubs.of('prepare') == stubs.of('store_final_sheet_and_changelog') == []
    assert result['inputs'] == []
    assert stubs.of('status')[-1]['phase'] == 'completed'


async def test_a_failing_activity_fails_the_job_and_says_so_in_the_status():
    stubs = Stubs(['speed 4000'], failing='apply')
    with pytest.raises(WorkflowFailureError) as failure:
        await run(stubs)
    assert error_message(failure.value) == 'apply broke'
    last = stubs.of('status')[-1]
    assert last['phase'] == 'failed'
    assert last['error'] == 'apply broke'
    assert stubs.of('store_final_sheet_and_changelog') == []
