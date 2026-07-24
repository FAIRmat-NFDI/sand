from nomad.config.models.plugins import APIEntryPoint


class SandAPIEntryPoint(APIEntryPoint):
    groq_api_key: str = ''
    whisper_model: str = 'whisper-large-v3-turbo'
    blablador_api_key: str = ''
    extraction_model: str = 'alias-large'
    nomad_base_url: str = 'https://nomad-lab.eu/prod/v1/api/v1'

    def load(self):
        from sand.apis.sand_api import app

        return app


sand_api = SandAPIEntryPoint(
    prefix='sand',
    name='SAND API',
    description='Structured Audio NOMAD Data - voice/text AI assistant for extracting lab process data.',
)
