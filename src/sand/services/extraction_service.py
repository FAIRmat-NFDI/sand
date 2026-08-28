import asyncio
from uuid import uuid4

# The queue nomad-llm-extraction's action entry point registers on
# (nomad.actions TaskQueue.CPU).
DEFAULT_TASK_QUEUE = 'cpu-task-queue'


class ExtractionError(RuntimeError):
    def __init__(self, message: str, raw_output: str = '', retries: int = 0) -> None:
        self.raw_output = raw_output
        self.retries = retries
        super().__init__(message)


class ExtractionService:
    def __init__(
        self,
        model_name: str,
        api_key: str,
        task_queue: str = DEFAULT_TASK_QUEUE,
        timeout_s: float = 600.0,
    ) -> None:
        self._model_name = model_name
        self._api_key = api_key
        self._task_queue = task_queue
        self._timeout_s = timeout_s

    async def extract(
        self,
        text: str,
        extraction_schema: dict,
        system_prompt: str,
        instruction_text: str = '',
    ) -> dict:
        from nomad.actions.client import get_client
        from nomad_llm_extraction.pipeline.models import (
            ExtractionWorkflowInput,
            LLMEngineConfig,
        )
        from nomad_llm_extraction.pipeline.workflows import ExtractionWorkflow

        workflow_input = ExtractionWorkflowInput(
            text=text,
            extraction_schema=extraction_schema,
            system_prompt=system_prompt,
            instruction_text=instruction_text,
            llm_engine_config=LLMEngineConfig(
                model_name=self._model_name, api_key=self._api_key or None
            ),
        )

        # see issue #19, todo
        client = await get_client()
        result = await asyncio.wait_for(
            client.execute_workflow(
                ExtractionWorkflow.run,
                workflow_input,
                id=f'sand-extract-{uuid4()}',
                task_queue=self._task_queue,
            ),
            timeout=self._timeout_s,
        )

        if result.err_message:
            raise ExtractionError(
                result.err_message,
                raw_output=result.raw_output,
                retries=result.retries,
            )
        return result.extracted_data
