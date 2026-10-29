"""What sand's actions share: where the LLM children run and how errors
are read."""

from datetime import timedelta

# nomad-llm-extraction registers its ExtractionWorkflow here; children are
# started BY NAME so sand never imports the peer plugin.
LLM_TASK_QUEUE = 'cpu-task-queue'
LLM_CHILD_TIMEOUT = timedelta(minutes=10)


def error_message(exc: BaseException) -> str:
    """The innermost cause's message (Temporal wraps errors per layer)."""
    cause: BaseException = exc
    while getattr(cause, 'cause', None) is not None:
        cause = cause.cause
    return getattr(cause, 'message', None) or str(cause)
