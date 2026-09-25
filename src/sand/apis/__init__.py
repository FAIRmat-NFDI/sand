from nomad.config.models.plugins import DashboardEntryPoint


class SandDashboardEntryPoint(DashboardEntryPoint):
    nomad_base_url: str = 'https://nomad-lab.eu/prod/v1/api/v1'

    llm_model_name: str = 'gemini/gemini-2.5-flash'
    # Deprecated, unused: the key lives in the action worker's environment
    # (LiteLLM provider var, e.g. GEMINI_API_KEY). Kept so existing
    # nomad.yaml files still load.
    llm_api_key: str = ''

    # Live transcription while recording (empty key = feature off).
    # Falls back to the DEEPGRAM_API_KEY environment variable.
    deepgram_api_key: str = ''
    deepgram_model: str = 'nova-3'
    # Default state of the GUI's "save live transcript" toggle: on = the
    # live text is stored and whisper is skipped; off = the live text is
    # display-only and whisper (Groq) transcribes the uploaded audio -
    # streaming quality is below batch (issue #47). The user decides per
    # recording; this only sets the default.
    store_live_transcript: bool = False

    def load(self):
        from sand.apis.sand_api import app

        return app


# The object keeps its name so the entry point id stays 'sand.apis:sand_api'
# and existing nomad.yaml configs keep working.
sand_api = SandDashboardEntryPoint(
    id_url_safe='sand',
    name='SAND',
    description='Structured Audio NOMAD Data - voice/text AI assistant for extracting lab process data.',
    # Tab first: a full window suits recording in the lab. Embedded, sand
    # uses NOMAD's session cookie instead of its own Keycloak login.
    launch_modes=['tab', 'embedded'],  # todo change here later
)
