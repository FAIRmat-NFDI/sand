"""The sheet-editing orchestrator: every input of the collection, in
order, each interpreted against the sheet the earlier ones left.

    (the API's reservation "starting" stands until the first status)
    activity collect_edit_inputs ──▶ {texts[n], input_entry_ids, model}
    activity replay_cached_inputs ──▶ the leading inputs the model already
        answered on their sheet, applied in one go: r reports; with r = n
        the final sheet stored there too, and the job is done
    status "interpreting r/n"
      per input k > r, IN ORDER (input k reads the sheet after k-1):
        activity prepare_input(k) ──▶ cached, or the two prompts
        (a status only if the last is older than HEARTBEAT)
        two child ExtractionWorkflows IN PARALLEL (the two opinions)
        activity agree_cache_and_apply_input(k, answers)
            ──▶ the user's preview, edits ready to confirm, confirmed ones
            written; its cache file and status "interpreting k/n" in one
            request
        activity replay_cached_inputs(after k) ──▶ the inputs after k whose
            answers still hold, in one go (an input changed above re-reads
            only what it touches); with all of them, the sheet stored there
    activity store_final_sheet_and_changelog ──▶ changelog checked; the sheet,
        changelog, cache and status "completed" {final, changed, changes,
        changelog, written} stored in one request
    on ANY failure: status "failed" {error}, then re-raise

A model answer the child cannot get (its error message) is one input's
problem: that input is shown as unreadable and the job goes on. An
activity or child that fails outright fails the job. The browser's only
bridge to all of this is the status file in the upload; a rerun after a
confirmation costs model calls only for inputs whose text or sheet
changed (see session.py), and the cached inputs between those are
replayed from the cache in one activity instead of two each.
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
        replay_cached_inputs,
        store_final_sheet_and_changelog,
        write_edit_status,
    )
    from sand.actions.edit.models import (
        ApplyInput,
        EditInput,
        JobInput,
        LlmAnswer,
        PrepareInput,
        ReplayInput,
        StoreInput,
        WriteStatusInput,
    )
    from sand.sheet_edit.resolve import PROMPT_VERSION

# the two opinions must differ by what they read, not by chance
TEMPERATURE = 0
# a status at least this often while notes are read: the API takes a
# status older than STALE_JOB_S (30 min) for a dead job and opens the lock
HEARTBEAT = timedelta(minutes=5)


@workflow.defn
class EditSheetWorkflow:
    @workflow.run
    async def run(self, data: EditInput) -> dict:  # noqa: PLR0912, PLR0915
        job_id = workflow.info().workflow_id
        reports: list[dict] = []  # one per finished input, in order
        # when the status was last written; the API's reservation is
        # written just before the job starts
        last_written = [workflow.info().start_time]

        def status_of(payload: dict) -> dict:
            return {
                'job_id': job_id,
                'collection_entry_id': data.collection_entry_id,
                # tells this job's reservation from a newer job's (_ours)
                'started_at': workflow.info().start_time.isoformat(),
                'updated_at': workflow.now().isoformat(),
                'prompt_version': PROMPT_VERSION,
                'temperature': TEMPERATURE,
                'inputs': list(reports),
                **payload,
            }

        async def status(payload: dict) -> None:
            await workflow.execute_activity(
                write_edit_status,
                WriteStatusInput(
                    upload_id=data.upload_id,
                    user_id=data.user_id,
                    status=status_of(payload),
                ),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            last_written[0] = workflow.now()

        async def heartbeat(payload: dict) -> None:
            """A status only when the last one is getting old: progress is
            told by the writes of each input."""
            if workflow.now() - last_written[0] >= HEARTBEAT:
                await status(payload)

        try:
            # the reservation says "starting" until the first note is read
            # or the job is done: a write less, the lock as before
            quiet = workflow.patched('one-step-when-cached')
            if not quiet:
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
                # absent from a job collected before rejections existed
                rejections=collected.get('rejections', []),
                input_ids=collected['input_entry_ids'],
            )
            total = len(job.texts)
            if len(collected['input_entry_ids']) != total:
                raise ApplicationError(
                    'inputs and entry ids differ', non_retryable=True
                )
            # a job started before the replay activity existed keeps its
            # old activity sequence (Temporal replays its history on a
            # worker restart; a new activity there is a non-determinism)
            replay_known = workflow.patched('replay-cached')
            replay = {'replayed': 0, 'reports': []}
            if replay_known and total:
                replay = await self._replay_cached(
                    job,
                    collected['input_entry_ids'],
                    # every input cached: the replay stores the sheet too
                    status_of({'model': job.llm_model_name}) if quiet else {},
                    None if quiet else status,
                )
            replayed = replay['replayed']
            reports.extend(replay['reports'])
            if 'done' in replay:
                return {'job_id': job_id, 'inputs': reports, **replay['done']}
            progress = {
                'phase': 'interpreting',
                'inputs_total': total,
                'inputs_replayed': replayed,
                'model': job.llm_model_name,
            }
            if not replay_known or replayed < total:
                await status({**progress, 'inputs_done': replayed})

            # each input's report goes with its cache file, one write; a job
            # started before writes a status before and after each input
            per_note = workflow.patched('one-write-per-note')
            after_read = workflow.patched('replay-after-read')
            for number, entry_id in enumerate(collected['input_entry_ids'], 1):
                if number <= replayed:
                    continue
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
                before = {**progress, 'inputs_done': number - 1}
                await (heartbeat(before) if per_note else status(before))
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
                        status=status_of({**progress, 'inputs_done': number})
                        if per_note
                        else {},
                    ),
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                reports.append(report)
                if per_note:
                    last_written[0] = workflow.now()
                else:
                    await status({**progress, 'inputs_done': number})
                if after_read and number < total:
                    # the inputs after it whose answers still hold: one step,
                    # and with all of them the sheet stored too
                    more = await self._replay_cached(
                        job,
                        collected['input_entry_ids'],
                        status_of({'model': job.llm_model_name}),
                        None,
                        after=number,
                    )
                    reports.extend(more['reports'])
                    replayed = more['replayed']
                    if 'done' in more:
                        return {'job_id': job_id, 'inputs': reports, **more['done']}

            writing = {'phase': 'writing-sheet', 'model': job.llm_model_name}
            await (heartbeat(writing) if per_note else status(writing))
            # the completed status goes with the final sheet, in one write;
            # a job started before writes it after (its history's order)
            together = workflow.patched('one-write-at-the-end')
            done = await workflow.execute_activity(
                store_final_sheet_and_changelog,
                StoreInput(
                    **job.model_dump(),
                    status=status_of({'model': job.llm_model_name}) if together else {},
                ),
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            if not together:
                await status(
                    {'phase': 'completed', 'model': job.llm_model_name, **done}
                )
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
    async def _replay_cached(
        job: JobInput, entry_ids: list[str], store_status: dict, status, after=0
    ) -> dict:
        """The leading cached inputs; with every input cached and a
        `store_status`, the final sheet stored too (`done`). `status`
        writes the "replaying" status; None: not written. The first
        `after` inputs are reported already: only the ones after them are."""
        total = len(job.texts)
        if status:
            await status(
                {
                    'phase': 'replaying',
                    'inputs_total': total,
                    'model': job.llm_model_name,
                }
            )
        replay = await workflow.execute_activity(
            replay_cached_inputs,
            ReplayInput(
                **job.model_dump(),
                entry_ids=entry_ids,
                store_status=store_status,
                after=after,
            ),
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        replayed = replay['replayed']
        # the inputs up to `after` were read in this job: they are cached
        if not after <= replayed <= total or len(replay['reports']) != replayed - after:
            raise ApplicationError(
                f'replayed {replayed} of {total} inputs with '
                f'{len(replay["reports"])} reports',
                non_retryable=True,
            )
        return replay

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
