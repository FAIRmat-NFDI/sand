"""Histories of jobs started on earlier workflow code must still replay:
a worker updated under a running job otherwise fails its workflow tasks
forever (Temporal nondeterminism)."""

from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

from sand.actions.edit.workflows import EditSheetWorkflow

HISTORIES = Path(__file__).parent / 'histories'
# file -> the job id it was recorded under (child workflow ids derive from it)
RECORDED = {
    # kit_lab before PR #127: no status snapshot before an input's LLM calls;
    # two inputs, the second one cached; stub activities of test_edit_workflow.py
    'edit_sheet_before_input_snapshot.json': 'job-before-input-snapshot',
}

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('history', sorted(RECORDED))
async def test_history_of_an_earlier_version_replays(history):
    text = (HISTORIES / history).read_text()
    replayer = Replayer(
        workflows=[EditSheetWorkflow], data_converter=pydantic_data_converter
    )
    await replayer.replay_workflow(WorkflowHistory.from_json(RECORDED[history], text))


def test_every_recorded_history_is_listed():
    assert sorted(p.name for p in HISTORIES.glob('*.json')) == sorted(RECORDED)
