"""The asynchronous extraction orchestrator (issue #19).

Processes and connections:

    BROWSER ── POST /extract-async ──▶ sand API ── start_action ──▶ TEMPORAL
       │◀───── {job_id} ─────────────────┘                            │
       │ polls GET /extract-status every ~3 s                        ▼
       │                                                     cpu action worker
       │                                                  ExtractHysprintWorkflow
       │                                                  ExtractionWorkflow (llm)
       │                                                   env: GEMINI_API_KEY
       │                                                     │            │
       ▼        status.json, xlsx, extracted.json            ▼            ▼
     NOMAD ◀────── HTTP + minted user token ──────────── activities   Gemini API

Inside ExtractHysprintWorkflow (this file):

    status "collecting"
    activity collect_and_route ──▶ {info, step_texts[n], select_schema}
    status "extracting 0/n"
      per step, n in PARALLEL:
        child ExtractionWorkflow(SELECT) ─▶ step_type
        activity make_fill_schema(step_type)
        child ExtractionWorkflow(FILL)   ─▶ slot ─▶ normalize_variants
        status "extracting k/n"
    status "writing-sheet"
    activity assemble_and_store  (assemble ▶ sheet ▶ xlsx ▶ add_derived_sheet)
    status "completed" {entry_id, step_types, issues}
    on ANY failure: status "failed" {error}, then re-raise

Temporal journals every activity result and child return: a worker crash
replays the journal and resumes exactly where it stopped. The browser's
only bridge to all of this is the status file in the upload.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from sand.actions.extract.activities import (
        assemble_and_store,
        collect_and_route,
        make_fill_schema,
        write_extraction_status,
    )
    from sand.actions.extract.models import ExtractInput, StoreInput, WriteStatusInput
    from sand.hysprint.steps import FILL_SYSTEM, SELECT_SYSTEM, normalize_variants

# nomad-llm-extraction registers its ExtractionWorkflow here; children are
# started BY NAME so sand never imports the peer plugin.
LLM_TASK_QUEUE = 'cpu-task-queue'
LLM_CHILD_TIMEOUT = timedelta(minutes=10)


def _error_message(exc: BaseException) -> str:
    """The innermost cause's message (Temporal wraps errors per layer)."""
    cause: BaseException = exc
    while getattr(cause, 'cause', None) is not None:
        cause = cause.cause
    return getattr(cause, 'message', None) or str(cause)


@workflow.defn
class ExtractHysprintWorkflow:
    """The extraction orchestrator: collect inputs, run one
    SELECT and one FILL child workflow per step (steps in parallel),
    assemble and store the sheet, and write progress into the upload's
    status file at every phase."""

    @workflow.run
    async def run(self, data: ExtractInput) -> dict:
        job_id = workflow.info().workflow_id
        # one entry per finished step, in completion order; carried in
        # every snapshot so the final (or failed) status still shows
        # which steps got through
        finished_steps: list[dict] = []
        # Steps complete concurrently: the lock serializes snapshot
        # builds AND writes, so an older snapshot can never land after
        # (and overwrite) a newer one. The terminal latch keeps
        # still-running step tasks from writing past completed/failed.
        status_lock = asyncio.Lock()
        terminal_written = False

        async def status(payload: dict, final: bool = False) -> None:
            nonlocal terminal_written
            async with status_lock:
                if terminal_written:
                    return
                terminal_written = final
                await workflow.execute_activity(
                    write_extraction_status,
                    WriteStatusInput(
                        upload_id=data.upload_id,
                        user_id=data.user_id,
                        status={
                            'job_id': job_id,
                            'collection_entry_id': data.collection_entry_id,
                            'updated_at': workflow.now().isoformat(),
                            'steps': list(finished_steps),
                            **payload,
                        },
                    ),
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )

        try:
            await status({'phase': 'collecting'})
            collected = await workflow.execute_activity(
                collect_and_route,
                data,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            step_texts = collected['step_texts']
            total = len(step_texts)
            done = 0
            await status({'phase': 'extracting', 'steps_done': 0, 'steps_total': total})

            def child_input(text: str, schema: dict, system: str, instruction=''):
                return {
                    'text': text,
                    'extraction_schema': schema,
                    'system_prompt': system,
                    'instruction_text': instruction,
                    # no api_key: the child's LiteLLM engine reads the
                    # provider env var on the worker (GEMINI_API_KEY, ...),
                    'llm_engine_config': {
                        'model_name': collected['llm_model_name'],
                    },
                }

            async def run_child(payload: dict, child_id: str) -> dict:
                result = await workflow.execute_child_workflow(
                    'ExtractionWorkflow',
                    payload,
                    id=child_id,
                    task_queue=LLM_TASK_QUEUE,
                    execution_timeout=LLM_CHILD_TIMEOUT,
                )
                if result.get('err_message'):
                    raise ApplicationError(result['err_message'], non_retryable=True)
                return result.get('extracted_data') or {}

            async def extract_one(index: int, text: str) -> dict:
                nonlocal done
                step_name = f'step {index + 1} ({text[:60]!r})'
                try:
                    selected = await run_child(
                        child_input(text, collected['select_schema'], SELECT_SYSTEM),
                        f'{job_id}-step{index + 1}-select',
                    )
                    step_type = selected['step_type']
                    fill = await workflow.execute_activity(
                        make_fill_schema,
                        step_type,
                        start_to_close_timeout=timedelta(minutes=1),
                        retry_policy=RetryPolicy(maximum_attempts=2),
                    )
                    slot = await run_child(
                        child_input(text, fill, FILL_SYSTEM, f'STEP TYPE: {step_type}'),
                        f'{job_id}-step{index + 1}-fill',
                    )
                except Exception as exc:
                    raise ApplicationError(
                        f'LLM extraction failed for {step_name}: {_error_message(exc)}',
                        non_retryable=True,
                    ) from exc
                done += 1
                finished_steps.append(
                    {
                        'step': index + 1,
                        'step_type': step_type,
                        'finished_at': workflow.now().isoformat(),
                    }
                )
                await status(
                    {'phase': 'extracting', 'steps_done': done, 'steps_total': total}
                )
                return normalize_variants(slot)

            slots = list(
                await asyncio.gather(
                    *(extract_one(i, text) for i, text in enumerate(step_texts))
                )
            )

            await status({'phase': 'writing-sheet'})
            stored = await workflow.execute_activity(
                assemble_and_store,
                StoreInput(
                    upload_id=data.upload_id,
                    user_id=data.user_id,
                    collection_entry_id=data.collection_entry_id,
                    info=collected['info'],
                    slots=slots,
                    input_entry_ids=collected['input_entry_ids'],
                ),
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            await status({'phase': 'completed', **stored}, final=True)
            return {'job_id': job_id, **stored}
        except Exception as exc:
            # Compensation, not error handling (the voice-eln pattern): make
            # the failure visible to the poll, then re-raise so Temporal
            # records the real error. Best-effort - never masks the original.
            try:
                await status(
                    {'phase': 'failed', 'error': _error_message(exc)}, final=True
                )
            except Exception:
                workflow.logger.exception('could not write the failed status')
            raise
