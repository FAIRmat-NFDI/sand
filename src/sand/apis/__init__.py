from nomad.config.models.plugins import APIEntryPoint


class SandAPIEntryPoint(APIEntryPoint):
    nomad_base_url: str = 'https://nomad-lab.eu/prod/v1/api/v1'

    llm_model_name: str = 'gemini/gemini-2.5-flash'
    llm_api_key: str = ''

    # Live transcription while recording (empty key = feature off).
    # Falls back to the DEEPGRAM_API_KEY environment variable.
    deepgram_api_key: str = ''
    deepgram_model: str = 'nova-3'
    # Store the live transcript and skip the whisper transcription.
    # Off: the live text is display-only and whisper (Groq) transcribes
    # the uploaded audio - streaming quality is below batch (issue #47).
    store_live_transcript: bool = False

    def load(self):
        from sand.apis.sand_api import app

        return app


sand_api = SandAPIEntryPoint(
    prefix='sand',
    name='SAND API',
    description='Structured Audio NOMAD Data - voice/text AI assistant for extracting lab process data.',
)
