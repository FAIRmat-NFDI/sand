from nomad.config.models.plugins import APIEntryPoint


class SandAPIEntryPoint(APIEntryPoint):
    # Speech-to-text is owned by the voice-eln plugin (its transcription action
    # runs inside NOMAD; the Groq key lives on the action worker) - sand only
    # creates AudioInput entries and links the user to them.
    anthropic_api_key: str = ''
    anthropic_model: str = 'claude-sonnet-4-20250514'
    nomad_base_url: str = 'https://nomad-lab.eu/prod/v1/api/v1'

    def load(self):
        from sand.apis.sand_api import app

        return app


sand_api = SandAPIEntryPoint(
    prefix='sand',
    name='SAND API',
    description='Structured Audio NOMAD Data - voice/text AI assistant for extracting lab process data.',
)
