"""Run one LLM extraction on NOMAD's Temporal infrastructure.

The nomad-llm-extraction plugin's action entry point registers its
workflows and activities on NOMAD's action worker (cpu-task-queue), so
sand runs no worker of its own: it starts an ExtractionWorkflow there
and awaits the result.
"""

import asyncio
from uuid import uuid4

# The queue nomad-llm-extraction's action entry point registers on
# (nomad.actions TaskQueue.CPU).
DEFAULT_TASK_QUEUE = 'cpu-task-queue'


class ExtractionError(RuntimeError):
    """The extraction workflow finished without valid extracted data."""

    def __init__(self, message: str, raw_output: str = '', retries: int = 0) -> None:
        self.raw_output = raw_output
        self.retries = retries
        super().__init__(message)


class ExtractionRunner:
    """One extract() call = one ExtractionWorkflow execution, unique id.

    `client_factory` is awaited to get a temporalio client; it defaults to
    nomad.actions.client.get_client (address/TLS/OIDC and the pydantic
    payload converter all come from NOMAD's own config).
    """

    def __init__(
        self,
        model_name: str,
        api_key: str,
        task_queue: str = DEFAULT_TASK_QUEUE,
        timeout_s: float = 600.0,
        client_factory=None,
    ) -> None:
        self._model_name = model_name
        self._api_key = api_key
        self._task_queue = task_queue
        self._timeout_s = timeout_s
        self._client_factory = client_factory

    async def extract(
        self,
        text: str,
        extraction_schema: dict,
        system_prompt: str,
        instruction_text: str = '',
    ) -> dict:
        """One text + one JSON schema -> the validated extracted dict.

        The workflow builds the prompt, calls the LLM (LiteLLM), parses and
        validates the JSON against the schema, and self-corrects on failure;
        an exhausted retry budget surfaces here as ExtractionError.
        """
        # Deferred: only needed when extraction actually runs, and the
        # package is only present where the plugin is installed.
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

        client = await self._get_client()
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

    async def _get_client(self):
        if self._client_factory is not None:
            return await self._client_factory()
        from nomad.actions.client import get_client

        return await get_client()
