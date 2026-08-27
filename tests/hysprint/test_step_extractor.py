import pytest

from sand.hysprint.step_extractor import extract_step, fill_step, select_step
from sand.hysprint.steps import FILL_SYSTEM, SELECT_SYSTEM
from sand.services.extraction_runner import ExtractionError


class StubRunner:
    """Records extract() calls and returns queued responses (or raises them)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def extract(
        self, text, extraction_schema, system_prompt, instruction_text=''
    ):
        self.calls.append(
            {
                'text': text,
                'schema': extraction_schema,
                'system_prompt': system_prompt,
                'instruction_text': instruction_text,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


NARRATION = 'spin coated NiOx on samples 1 and 2 at 3000 rpm'


@pytest.mark.asyncio
async def test_select_step_uses_the_enum_schema_and_prompt():
    runner = StubRunner([{'step_type': 'Spin Coating'}])

    step_type = await select_step(runner, NARRATION)

    assert step_type == 'Spin Coating'
    call = runner.calls[0]
    assert call['system_prompt'] == SELECT_SYSTEM
    assert 'Spin Coating' in call['schema']['properties']['step_type']['enum']
    assert call['text'] == NARRATION


@pytest.mark.asyncio
async def test_fill_step_slices_the_schema_and_normalizes_labels():
    runner = StubRunner(
        [
            {
                'step_type': 'Spin Coating',
                'variants': [{'samples': ['sample 1', '2'], 'material_name': 'NiOx'}],
            }
        ]
    )

    slot = await fill_step(runner, NARRATION, 'Spin Coating')

    assert slot['variants'][0]['samples'] == ['1', '2']
    call = runner.calls[0]
    assert call['system_prompt'] == FILL_SYSTEM
    assert call['instruction_text'] == 'STEP TYPE: Spin Coating'
    assert call['schema']['properties']['step_type'] == {'const': 'Spin Coating'}


@pytest.mark.asyncio
async def test_extract_step_selects_then_fills():
    runner = StubRunner(
        [
            {'step_type': 'Spin Coating'},
            {'step_type': 'Spin Coating', 'variants': [{'samples': 'all'}]},
        ]
    )

    slot = await extract_step(runner, NARRATION)

    assert slot == {'step_type': 'Spin Coating', 'variants': [{'samples': 'all'}]}
    expected_calls = 2  # one select, one fill
    assert len(runner.calls) == expected_calls
    # both calls carry the same narration; only schema/prompt differ
    assert {c['text'] for c in runner.calls} == {NARRATION}


@pytest.mark.asyncio
async def test_extraction_errors_propagate():
    runner = StubRunner([ExtractionError('Max retry attempts reached.')])

    with pytest.raises(ExtractionError, match='Max retry'):
        await extract_step(runner, NARRATION)
