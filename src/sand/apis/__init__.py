from nomad.config.models.plugins import APIEntryPoint


class SandAPIEntryPoint(APIEntryPoint):
    nomad_base_url: str = 'https://nomad-lab.eu/prod/v1/api/v1'

    llm_model_name: str = 'gemini/gemini-2.5-flash'
    llm_api_key: str = ''

    def load(self):
        from sand.apis.sand_api import app

        return app


sand_api = SandAPIEntryPoint(
    prefix='sand',
    name='SAND API',
    description='Structured Audio NOMAD Data - voice/text AI assistant for extracting lab process data.',
)
