from nomad.actions import TaskQueue
from pydantic import Field
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from nomad.config.models.plugins import ActionEntryPoint


class ExtractActionEntryPoint(ActionEntryPoint):
    task_queue: str = Field(
        default=TaskQueue.CPU,
        description='Task queue for the hysprint extraction action.',
    )

    def load(self):
        from nomad.actions import Action

        from sand.actions.extract.activities import write_extraction_status
        from sand.actions.extract.workflows import ExtractHysprintWorkflow

        return Action(
            task_queue=self.task_queue,
            workflow=ExtractHysprintWorkflow,
            activities=[write_extraction_status],
        )


extract_action_entry_point = ExtractActionEntryPoint(
    name='ExtractHysprintAction',
    description='Extract an experiment collection into the hysprint sheet, '
    'asynchronously.',
)
