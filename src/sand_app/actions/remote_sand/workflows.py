from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from nomad.actions.manager import (
        RequestUserInputActivityInput,
        request_user_input_activity,
    )

    from sand_app.actions.remote_sand.activities import (
        remote_schema_extraction as schema_extraction,
    )
    from sand_app.actions.remote_sand.activities import (
        remote_sst as sst,
    )
    from sand_app.actions.remote_sand.activities import (
        remote_upload_entry_activity as upload_entry,
    )
    from sand_app.actions.remote_sand.models import (
        RemoteSandWorkflowInput,
        TextVerificationDecision,
    )


@workflow.defn
class RemoteSandWorkflow:
    def __init__(self):
        self._decision: TextVerificationDecision | None = None

    @workflow.signal
    def verify_text(self, decision: TextVerificationDecision) -> None:
        self._decision = decision

    @workflow.run
    async def run(self, data: RemoteSandWorkflowInput) -> dict:
        retry_policy = RetryPolicy(maximum_attempts=3)

        # 1. SST Activity (Groq)
        transcribed_text = await workflow.execute_activity(
            sst,
            data,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry_policy,
        )

        # 2. Human-in-the-loop Text Verification
        await workflow.execute_activity(
            request_user_input_activity,
            RequestUserInputActivityInput(
                action_instance_id=workflow.info().workflow_id,
                user_id=data.user_id,
                signal_fn_name='verify_text',
                title='Review Transcribed Text',
                description='Please verify the text below.',
                initial_data={'verified_text': transcribed_text},
            ),
            start_to_close_timeout=timedelta(seconds=10),
        )

        await workflow.wait_condition(
            lambda: self._decision is not None,
            timeout=timedelta(hours=24),
        )

        if self._decision.decision.lower() != 'approve':
            return {'status': 'rejected'}

        final_text = self._decision.verified_text

        # 3. Schema Extraction Activity
        schema_data = await workflow.execute_activity(
            schema_extraction,
            args=[final_text, data.ai_model],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry_policy,
        )

        # 4. Upload Entry Activity
        upload_result = await workflow.execute_activity(
            upload_entry,
            args=[schema_data, data.upload_id, data.user_id],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=10_0000),
        )

        return {
            'status': 'approved',
            'upload_result': upload_result,
            'schema_data': schema_data,
        }
