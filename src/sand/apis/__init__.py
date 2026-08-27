from nomad.config.models.plugins import APIEntryPoint


class SandAPIEntryPoint(APIEntryPoint):
    # Speech-to-text is owned by the voice-eln plugin (its transcription action
    # runs inside NOMAD; the Groq key lives on the action worker) - sand only
    # creates AudioInput entries and links the user to them.
    nomad_base_url: str = 'https://nomad-lab.eu/prod/v1/api/v1'
    # LLM used for step extraction, run through the nomad-llm-extraction
    # plugin's workflows (LiteLLM model notation). The key travels inside
    # the workflow input, so it is configured here, not on the worker.
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
