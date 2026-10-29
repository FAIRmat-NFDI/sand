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


TOKEN_EXPIRES_S = 15 * 60


def voice_service():
    from nomad.config import config

    from sand.services.voice_eln import VoiceElnService

    entry_point = config.get_plugin_entry_point('sand.apis:sand_api')
    return VoiceElnService(base_url=entry_point.nomad_base_url)


def user_token(user_id: str) -> str:
    from nomad.auth.tokens import generate_simple_token

    return generate_simple_token(user_id, TOKEN_EXPIRES_S)


def llm_model_name() -> str:
    from nomad.config import config

    return config.get_plugin_entry_point('sand.apis:sand_api').llm_model_name
