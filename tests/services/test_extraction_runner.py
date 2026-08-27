import pytest

from sand.services.extraction_runner import ExtractionError, ExtractionRunner

# The runner builds nomad-llm-extraction's workflow input; without the
# package (installed with the plugin) these tests cannot run.
models = pytest.importorskip('nomad_llm_extraction.pipeline.models')


class FakeClient:
    """Captures execute_workflow calls and returns a canned output."""

    def __init__(self, output):
        self.output = output
        self.calls = []

    async def execute_workflow(self, run, workflow_input, *, id, task_queue):
        self.calls.append({'input': workflow_input, 'id': id, 'task_queue': task_queue})
        return self.output


def _runner(client) -> ExtractionRunner:
    async def factory():
        return client

    return ExtractionRunner(
        model_name='gemini/gemini-2.5-flash',
        api_key='key-1',
        client_factory=factory,
    )


def _output(**overrides):
    return models.ExtractionWorkflowOutput(
        extracted_data={'step_type': 'Cleaning'}, raw_output='{}', **overrides
    )


@pytest.mark.asyncio
async def test_extract_builds_the_workflow_input():
    client = FakeClient(_output())
    result = await _runner(client).extract(
        text='cleaned it',
        extraction_schema={'type': 'object'},
        system_prompt='SYS',
        instruction_text='INSTR',
    )

    assert result == {'step_type': 'Cleaning'}
    call = client.calls[0]
    assert call['task_queue'] == 'cpu-task-queue'
    workflow_input = call['input']
    assert workflow_input.text == 'cleaned it'
    assert workflow_input.extraction_schema == {'type': 'object'}
    assert workflow_input.system_prompt == 'SYS'
    assert workflow_input.instruction_text == 'INSTR'
    assert workflow_input.llm_engine_config.model_name == 'gemini/gemini-2.5-flash'
    assert workflow_input.llm_engine_config.api_key.get_secret_value() == 'key-1'


@pytest.mark.asyncio
async def test_each_call_gets_a_unique_workflow_id():
    client = FakeClient(_output())
    runner = _runner(client)

    await runner.extract('a', {}, 'SYS')
    await runner.extract('b', {}, 'SYS')

    ids = [c['id'] for c in client.calls]
    assert len(set(ids)) == len(ids) == 2  # noqa: PLR2004 - two calls above
    assert all(i.startswith('sand-extract-') for i in ids)


@pytest.mark.asyncio
async def test_err_message_raises_extraction_error():
    client = FakeClient(_output(err_message='Max retry attempts reached.', retries=3))

    with pytest.raises(ExtractionError, match='Max retry') as excinfo:
        await _runner(client).extract('a', {}, 'SYS')

    expected_retries = 3
    assert excinfo.value.retries == expected_retries
