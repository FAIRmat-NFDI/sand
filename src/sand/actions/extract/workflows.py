from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from sand.actions.extract.activities import write_extraction_status
    from sand.actions.extract.models import ExtractInput, WriteStatusInput


@workflow.defn
class ExtractHysprintWorkflow:
    """collect inputs ->
    per-step LLM child workflows -> assemble -> sheet write-back."""

    @workflow.run
    async def run(self, data: ExtractInput) -> dict:
        job_id = workflow.info().workflow_id

        async def status(phase: str) -> None:
            await workflow.execute_activity(
                write_extraction_status,
                WriteStatusInput(
                    upload_id=data.upload_id,
                    user_id=data.user_id,
                    status={
                        'job_id': job_id,
                        'phase': phase,
                        'collection_entry_id': data.collection_entry_id,
                        'updated_at': workflow.now().isoformat(),
                    },
                ),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        await status('skeleton-started')
        await status('skeleton-completed')
        return {'job_id': job_id}
