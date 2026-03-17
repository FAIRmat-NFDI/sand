from nomad.actions import TaskQueue
from pydantic import Field
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from nomad.config.models.plugins import ActionEntryPoint


class RemoteSandEntryPoint(ActionEntryPoint):
    task_queue: str = Field(
        default=TaskQueue.CPU, description='Determines the task queue for this action'
    )

    def load(self):
        from nomad.actions import Action

        from sand_app.actions.remote_sand.activities import (
            remote_schema_extraction,
            remote_sst,
            remote_upload_entry_activity,
        )
        from sand_app.actions.remote_sand.workflows import RemoteSandWorkflow

        return Action(
            task_queue=self.task_queue,
            workflow=RemoteSandWorkflow,
            activities=[
                remote_schema_extraction,
                remote_sst,
                remote_upload_entry_activity,
            ],
        )


remote_sand_action_entry_point = RemoteSandEntryPoint(
    name='RemoteSand',
    description='Remote sand action for speech-to-text, verification and schema extraction.',
)
