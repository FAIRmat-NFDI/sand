"""Worker-side NOMAD I/O for the extract action.

Activities mint a short-lived token for the requesting user
(generate_simple_token - the same mechanism behind NOMAD's app tokens)
and reuse the exact HTTP service code the API endpoints use, so the
worker needs no second implementation of the collect/write logic.
"""

from temporalio import activity

from sand.actions.extract.models import WriteStatusInput

TOKEN_EXPIRES_S = 15 * 60


def _voice_service():
    from nomad.config import config

    from sand.services.voice_eln import VoiceElnService

    entry_point = config.get_plugin_entry_point('sand.apis:sand_api')
    return VoiceElnService(base_url=entry_point.nomad_base_url)


def _user_token(user_id: str) -> str:
    from nomad.auth.tokens import generate_simple_token

    return generate_simple_token(user_id, TOKEN_EXPIRES_S)


@activity.defn
async def write_extraction_status(data: WriteStatusInput) -> None:
    """Write the status file into the experiment upload (the GUI polls it)."""
    from sand.hysprint.sheet import EXTRACTION_STATUS_MAINFILE

    voice = _voice_service()
    async with voice.build_client(_user_token(data.user_id)) as client:
        await voice.write_status_file(
            client, data.upload_id, EXTRACTION_STATUS_MAINFILE, data.status
        )
