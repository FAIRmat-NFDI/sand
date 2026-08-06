from nomad.config.models.plugins import APIEntryPoint


class SandAPIEntryPoint(APIEntryPoint):
    # Speech-to-text is owned by the voice-eln plugin (its transcription action
    # runs inside NOMAD; the Groq key lives on the action worker) - sand only
    # creates AudioInput entries and reads the transcript back.
    anthropic_api_key: str = ''
    anthropic_model: str = 'claude-sonnet-4-20250514'
    nomad_base_url: str = 'https://nomad-lab.eu/prod/v1/api/v1'
    # How long /transcribe waits for the voice-eln transcript before giving up
    # (the entry keeps transcribing in NOMAD regardless).
    transcript_timeout_s: float = 120.0

    def load(self):
        from sand.apis.sand_api import app

        return app


sand_api = SandAPIEntryPoint(
    prefix='sand',
    name='SAND API',
    description='Structured Audio NOMAD Data - voice/text AI assistant for extracting lab process data.',
)
