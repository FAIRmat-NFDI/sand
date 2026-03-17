from pydantic import Field
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from nomad.actions import TaskQueue
    from nomad.config.models.plugins import ActionEntryPoint


class LocalSandEntryPoint(ActionEntryPoint):
    task_queue: str = Field(
        default=TaskQueue.CPU, description='Determines the task queue for this action'
    )

    def load(self):
        from nomad.actions import Action

        from sand_app.actions.local_sand.activities import (
            local_schema_extraction,
            local_sst,
            local_upload_entry_activity,
        )
        from sand_app.actions.local_sand.workflows import LocalSandWorkflow

        return Action(
            task_queue=self.task_queue,
            workflow=LocalSandWorkflow,
            activities=[
                local_schema_extraction,
                local_sst,
                local_upload_entry_activity,
            ],
        )


local_sand_action_entry_point = LocalSandEntryPoint(
    name='LocalSand',
    description='Local sand action for speech-to-text, verification and schema extraction.',
)
