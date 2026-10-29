from nomad.actions import TaskQueue
from pydantic import Field
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from nomad.config.models.plugins import ActionEntryPoint


class EditActionEntryPoint(ActionEntryPoint):
    task_queue: str = Field(
        default=TaskQueue.CPU,
        description='Task queue for the sheet editing action.',
    )

    def load(self):
        from nomad.actions import Action

        from sand.actions.edit.activities import (
            collect_edit_inputs,
            finish_input,
            finish_job,
            prepare_input,
            write_edit_status,
        )
        from sand.actions.edit.workflows import EditSheetWorkflow

        return Action(
            task_queue=self.task_queue,
            workflow=EditSheetWorkflow,
            activities=[
                collect_edit_inputs,
                prepare_input,
                finish_input,
                finish_job,
                write_edit_status,
            ],
        )


edit_action_entry_point = EditActionEntryPoint(
    name='EditSheetAction',
    description='Apply the spoken or written edits of a collection to its '
    'uploaded sheet, asynchronously.',
)
