"""EditSheetWorkflow on Temporal's test server: the activities are stubs
that record what they got, the LLM child echoes its payload back."""

import asyncio
import json
import uuid
from dataclasses import dataclass, field

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from sand.actions.common import LLM_TASK_QUEUE, error_message
from sand.actions.edit.models import (
    EditInput,
    FinishInput,
    JobInput,
    PrepareInput,
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
        return {'extracted_data': {'got': payload}, 'err_message': None}


@dataclass
class Stubs:
    """The activities, recording every call; `cached` says which inputs
    prepare reports as already answered."""

    texts: list[str]
    cached: set[int] = field(default_factory=set)
    failing: str | None = None  # the activity that raises
    calls: list[tuple[str, object]] = field(default_factory=list)

    def activities(self):
        stubs = self

        @activity.defn(name='collect_edit_inputs')
        async def collect(data: EditInput) -> dict:
            stubs.record('collect', data)
            return {
                'texts': stubs.texts,
                'input_entry_ids': [f'e{i + 1}' for i in range(len(stubs.texts))],
                'llm_model_name': MODEL,
            }

        @activity.defn(name='prepare_input')
        async def prepare(data: PrepareInput) -> dict:
            stubs.record('prepare', data)
            if data.number in stubs.cached:
                return {'cached': True, 'prompts': []}
            text = data.texts[data.number - 1]
            return {
                'cached': False,
                'prompts': [{**p, 'text': f'{p["text"]} {text}'} for p in PROMPTS],
            }

        @activity.defn(name='finish_input')
        async def finish(data: FinishInput) -> dict:
            stubs.record('finish', data)
            return {'input': data.number, 'preview': f'preview {data.number}'}

        @activity.defn(name='finish_job')
        async def finish_job(data: JobInput) -> dict:
            stubs.record('finish_job', data)
            return {'final': 'abc', 'changed': True, 'changes': 3, 'changelog': 'log'}

        @activity.defn(name='write_edit_status')
        async def status(data: WriteStatusInput) -> None:
            stubs.record('status', data.status)

        return [collect, prepare, finish, finish_job, status]

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

    assert [name for name, _ in stubs.calls] == [
        'status',
        'collect',
        'status',
        'prepare',
        'finish',
        'status',
        'prepare',
        'finish',
        'status',
        'status',
        'finish_job',
        'status',
    ]
    first, second = stubs.of('finish')
    assert [(f.number, f.entry_id) for f in (first, second)] == [(1, 'e1'), (2, 'e2')]
    assert first.texts == second.texts == ['speed 4000', 'clear solvent 2']
    assert first.llm_model_name == MODEL
    assert first.job_id == result['job_id']

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
    assert [s['phase'] for s in statuses] == [
        'collecting',
        'interpreting',
        'interpreting',
        'interpreting',
        'writing-sheet',
        'completed',
    ]
    assert [s.get('inputs_done') for s in statuses[1:4]] == [0, 1, 2]
    assert all(s['inputs_total'] == 2 for s in statuses[1:4])  # noqa: PLR2004
    assert [len(s['inputs']) for s in statuses] == [0, 0, 1, 2, 2, 2]
    done = statuses[-1]
    assert done['collection_entry_id'] == 'c1'
    assert done['model'] == MODEL
    assert done['prompt_version'] == PROMPT_VERSION
    assert done['temperature'] == 0
    assert (done['final'], done['changed'], done['changes']) == ('abc', True, 3)
    assert done['changelog'] == 'log'
    assert done['job_id'] == statuses[0]['job_id']


async def test_a_cached_input_asks_no_child():
    stubs = Stubs(['speed 4000', 'clear solvent 2'], cached={1})
    await run(stubs)
    first, second = stubs.of('finish')
    assert first.answers == []
    assert len(second.answers) == 2  # noqa: PLR2004


async def test_an_answer_the_child_cannot_get_reaches_finish_as_an_error():
    stubs = Stubs(['FAIL this one', 'speed 4000'])
    await run(stubs)
    first, second = stubs.of('finish')
    assert [a.text for a in first.answers] == [None, None]
    assert [a.error for a in first.answers] == ['Max retry attempts reached.'] * 2
    assert [a.error for a in second.answers] == [None, None]


async def test_no_inputs_is_a_finished_job_with_nothing_to_do():
    stubs = Stubs([])
    result = await run(stubs)
    assert stubs.of('prepare') == []
    assert result['inputs'] == []
    assert stubs.of('status')[-1]['phase'] == 'completed'


async def test_a_failing_activity_fails_the_job_and_says_so_in_the_status():
    stubs = Stubs(['speed 4000'], failing='finish')
    with pytest.raises(WorkflowFailureError) as failure:
        await run(stubs)
    assert error_message(failure.value) == 'finish broke'
    last = stubs.of('status')[-1]
    assert last['phase'] == 'failed'
    assert last['error'] == 'finish broke'
    assert stubs.of('finish_job') == []
