"""The sheet-editing orchestrator: every input of the collection, in
order, each interpreted against the sheet the earlier ones left.

    status "collecting"
    activity collect_edit_inputs ──▶ {texts[n], input_entry_ids, model}
    status "interpreting 0/n"
      per input k, IN ORDER (input k reads the sheet after k-1):
        activity prepare_input(k) ──▶ cached, or the two prompts
        two child ExtractionWorkflows IN PARALLEL (the two opinions)
        activity agree_cache_and_apply_input(k, answers)
            ──▶ the user's preview, edits ready to confirm, confirmed ones written
        status "interpreting k/n"
    status "writing-sheet"
    activity store_final_sheet_and_changelog ──▶ changelog checked, sheet stored
    status "completed" {final, changed, changes, changelog}
    on ANY failure: status "failed" {error}, then re-raise

A model answer the child cannot get (its error message) is one input's
problem: that input is shown as unreadable and the job goes on. An
activity or child that fails outright fails the job. The browser's only
bridge to all of this is the status file in the upload; a rerun after a
confirmation costs model calls only for inputs whose text or sheet
changed (see session.py).
"""

import asyncio
import json
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from sand.actions.common import LLM_CHILD_TIMEOUT, LLM_TASK_QUEUE, error_message
    from sand.actions.edit.activities import (
        agree_cache_and_apply_input,
        collect_edit_inputs,
        prepare_input,
        store_final_sheet_and_changelog,
        write_edit_status,
    )
    from sand.actions.edit.models import (
        ApplyInput,
        EditInput,
        JobInput,
        LlmAnswer,
        PrepareInput,
        WriteStatusInput,
    )
    from sand.sheet_edit.resolve import PROMPT_VERSION

# the two opinions must differ by what they read, not by chance
TEMPERATURE = 0


@workflow.defn
class EditSheetWorkflow:
    @workflow.run
    async def run(self, data: EditInput) -> dict:
        job_id = workflow.info().workflow_id
        reports: list[dict] = []  # one per finished input, in order

        async def status(payload: dict) -> None:
            await workflow.execute_activity(
                write_edit_status,
                WriteStatusInput(
                    upload_id=data.upload_id,
                    user_id=data.user_id,
                    status={
                        'job_id': job_id,
                        'collection_entry_id': data.collection_entry_id,
                        'updated_at': workflow.now().isoformat(),
                        'prompt_version': PROMPT_VERSION,
                        'temperature': TEMPERATURE,
                        'inputs': list(reports),
                        **payload,
                    },
                ),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        try:
            await status({'phase': 'collecting'})
            collected = await workflow.execute_activity(
                collect_edit_inputs,
                data,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            job = JobInput(
                upload_id=data.upload_id,
                user_id=data.user_id,
                job_id=job_id,
                llm_model_name=collected['llm_model_name'],
                texts=collected['texts'],
                confirmations=collected['confirmations'],
            )
            total = len(job.texts)
            if len(collected['input_entry_ids']) != total:
                raise ApplicationError(
                    'inputs and entry ids differ', non_retryable=True
                )
            progress = {
                'phase': 'interpreting',
                'inputs_total': total,
                'model': job.llm_model_name,
            }
            await status({**progress, 'inputs_done': 0})

            for number, entry_id in enumerate(collected['input_entry_ids'], 1):
                prepared = await workflow.execute_activity(
                    prepare_input,
                    PrepareInput(**job.model_dump(), number=number),
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                if len(prepared['prompts']) not in (0, 2):
                    raise ApplicationError(
                        f'input {number}: {len(prepared["prompts"])} prompts, not 2',
                        non_retryable=True,
                    )
                # a snapshot between the activities: the API's staleness
                # guard must not open while one input is being worked on
                await status({**progress, 'inputs_done': number - 1})
                # the two opinions at once; none when the proposal is cached
                answers = await asyncio.gather(
                    *(
                        self._ask(
                            prompt, job.llm_model_name, f'{job_id}-input{number}-{name}'
                        )
                        for name, prompt in zip(('a', 'b'), prepared['prompts'])
                    )
                )
                report = await workflow.execute_activity(
                    agree_cache_and_apply_input,
                    ApplyInput(
                        **job.model_dump(),
                        number=number,
                        entry_id=entry_id,
                        answers=list(answers),
                    ),
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                reports.append(report)
                await status({**progress, 'inputs_done': number})

            await status({'phase': 'writing-sheet', 'model': job.llm_model_name})
            done = await workflow.execute_activity(
                store_final_sheet_and_changelog,
                job,
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            await status({'phase': 'completed', 'model': job.llm_model_name, **done})
            return {'job_id': job_id, 'inputs': reports, **done}
        except Exception as exc:
            # make the failure visible to the poll, then re-raise so Temporal
            # records the real error; best-effort, never masks the original
            try:
                await status({'phase': 'failed', 'error': error_message(exc)})
            except Exception:
                workflow.logger.exception('could not write the failed status')
            raise

    @staticmethod
    async def _ask(prompt: dict, model: str, child_id: str) -> LlmAnswer:
        """One opinion from nomad-llm-extraction's ExtractionWorkflow. The
        prompt is passed prebuilt, so the model reads exactly what
        resolve.py composed; the child parses and schema-checks the
        answer and retries with the error a few times. An answer it
        reports it could not get is this input's problem; a child that
        fails outright (no worker, provider down) raises and fails the
        job, so nothing is cached as unreadable for an infrastructure
        error."""
        result = await workflow.execute_child_workflow(
            'ExtractionWorkflow',
            {
                'prompt': f'{prompt["system"]}\n\n{prompt["text"]}',
                'extraction_schema': prompt['schema'],
                # no api_key: the child's engine reads the provider env var
                'llm_engine_config': {'model_name': model},
                'llm_engine_optional_params': {'temperature': TEMPERATURE},
            },
            id=child_id,
            task_queue=LLM_TASK_QUEUE,
            execution_timeout=LLM_CHILD_TIMEOUT,
        )
        if result.get('err_message'):
            return LlmAnswer(error=result['err_message'])
        # the child hands back the parsed JSON; read_answer wants the text
        return LlmAnswer(text=json.dumps(result.get('extracted_data')))
