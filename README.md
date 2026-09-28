# sand

Voice/text AI assistant for extracting structured lab process data into NOMAD.

## How it works

```
create an experiment in the SAND UI (with the experiment-info form)
  -> one NOMAD upload with an InputCollection entry
     (+ a WrittenNote at experiment_info.archive.json holding the form JSON)

record audio / save a step note in the SAND UI (experiment selected,
optionally with a free-text label)
  -> the file/note goes into the experiment upload
       -> the voice-eln plugin creates an AudioInput entry
          and transcribes it (Whisper, inside NOMAD)
  -> the entry is referenced from the experiment's InputCollection
  -> SAND shows a clickable link to the created entry
```

An **experiment** is one NOMAD upload holding a
[`nomad-voice-eln`](https://github.com/FAIRmat-NFDI/nomad-voice-eln)
`InputCollection` entry plus all the `AudioInput` and `WrittenNote` entries
that belong to it. The SAND dashboard lists the user's **unpublished**
experiments (published uploads are read-only); recordings and typed step notes
always attach to the selected experiment. SAND does **not** transcribe audio
itself — raw audio, machine transcript, and human corrections live in the
voice-eln entries.

## Running the SAND app

SAND is not a standalone application — it is a **NOMAD dashboard plugin**. NOMAD mounts it onto its API server under `dashboards/sand/`. To run it you
start a NOMAD instance with this plugin installed and configured. The easiest way
to do this for development is via the
[`nomad-distro-dev`](https://github.com/FAIRmat-NFDI/nomad-distro-dev) repository.

### Prerequisites

Before you can run the SAND app you need a few things in place:

- A working [`nomad-distro-dev`](https://github.com/FAIRmat-NFDI/nomad-distro-dev)
  checkout with its [basic infra prerequisites](https://github.com/FAIRmat-NFDI/nomad-distro-dev#basic-infra)
- The new NOMAD GUI (the `nomad-gui` package) **installed and enabled in the
  same NOMAD**. Only the new GUI lists dashboards, and SAND has no login of
  its own: it uses the GUI's.
- The [`nomad-voice-eln`](https://github.com/FAIRmat-NFDI/nomad-voice-eln) plugin
  **installed and enabled in the same NOMAD** — it owns audio entries and
  transcription. Follow its README for setup, including `GROQ_API_KEY` in the
  **action worker's environment** (speech-to-text runs there, not in SAND).
- The [`nomad-llm-extraction`](https://github.com/FAIRmat-NFDI/nomad-llm-extraction)
  plugin **installed and enabled in the same NOMAD** — its action entry point
  registers the extraction workflows on NOMAD's action worker; SAND only starts
  them there.
- The [`nomad-hysprint`](https://github.com/nomad-hzb/nomad-hysprint) plugin
  **installed and enabled in the same NOMAD** — its batch parser turns the
  sheet SAND writes into NOMAD entries.
- An **LLM API key** for the extraction model (Gemini by default; any LiteLLM
  model works), set as the provider's environment variable (e.g.
  `GEMINI_API_KEY`) in the **action worker's environment**. It is not
  configured in `nomad.yaml`.

### 1. Add the plugin to a NOMAD dev distribution

The SAND app is loaded as part of a NOMAD distribution, so the plugin first has
to live inside your `nomad-distro-dev` checkout as a workspace package. From the
root of `nomad-distro-dev`, add it under `packages/` (as a git submodule if you
have a repo for it) and register it with `uv`:

```sh
# Add the plugin source under packages/ (submodule shown here; a plain copy works too)
git submodule add https://github.com/FAIRmat-NFDI/sand.git packages/sand

# Register it as an editable workspace dependency
uv add packages/sand
```

This adds `nomad-sand` to `[project.dependencies]` and `[tool.uv.sources]` in the
distribution's `pyproject.toml` (with `nomad-sand = { workspace = true }`).

### 2. Configure the plugin in `nomad.yaml`

The `uv run poe setup` step (below) creates a `nomad.yaml` in the root of your
`nomad-distro-dev` checkout if one does not exist yet. You must edit it to
**enable** the entry points of SAND and of the plugins it builds on, and to set
SAND's options:

```yaml
plugins:
  entry_points:
    include:
      - sand.apis:sand_api
      - sand.actions.extract:extract_action_entry_point
      # the new NOMAD GUI: lists the dashboard, SAND uses its login
      - nomad_gui.apis:gui_api
      # audio and note entries, transcription
      - nomad_voice_eln.schema_packages:schema_package_entry_point
      - nomad_voice_eln.parsers:parser_entry_point
      - nomad_voice_eln.actions.transcribe:transcribe_action_entry_point
      - nomad_voice_eln.actions.record_note:record_note_action_entry_point
      # extraction
      - nomad_llm_extraction.actions:llm_extractor_action_entry_point
      # the sheet SAND writes is parsed into hysprint entries
      - nomad_hysprint.schema_packages:hysprint_package
      - nomad_hysprint.parsers:hysprint_experiment_parser
    options:
      sand.apis:sand_api:
        llm_model_name: 'gemini/gemini-2.5-flash'  # LiteLLM notation
        # The LLM key is NOT configured here: set the provider env var
        # (e.g. GEMINI_API_KEY for gemini/* models) in the environment of
        # the cpu action worker, next to voice-eln's GROQ_API_KEY.
        # Base URL of the NOMAD API the app uploads to. For a local instance:
        nomad_base_url: 'http://localhost:8000/nomad-oasis/api/v1'
        # Live transcription while recording (optional). Empty key = feature
        # off; also read from the DEEPGRAM_API_KEY environment variable.
        deepgram_api_key: '<your-deepgram-api-key>'
        deepgram_model: 'nova-3'
        # default of the GUI toggle "save live transcript instead of
        # running whisper" (the user decides per recording; default off)
        # store_live_transcript: true
        # SAND opens in a browser tab (also the default). It does not work
        # embedded in the GUI: the iframe blocks downloads.
        launch_modes: ['tab']
```

`include` is a whitelist: once it is set, NOMAD loads only the entry points
listed, so other plugins you use have to be listed too.

There is no Groq/Whisper configuration in SAND anymore: speech-to-text is done
by the voice-eln transcription action, and its `GROQ_API_KEY` lives in the
action worker's environment.


> [!WARNING]
> Do not commit real API keys to `nomad.yaml`. Keep them out of version control

### 3. Start NOMAD

From the root of your `nomad-distro-dev` checkout:

```sh
uv run poe setup

docker compose up -d

uv sync

uv run poe start

# in a second terminal: the action worker (transcription, extraction),
# with GROQ_API_KEY and the LLM key (e.g. GEMINI_API_KEY) in its environment
uv run poe cpuworker

# optional, in a third terminal: the old GUI on port 3000. SAND's
# "View in NOMAD" links still point to it.
uv run poe gui start
```

The new GUI needs no command of its own: NOMAD serves it at
`http://localhost:8000/nomad-oasis/gui/v2/`.

### 4. Open the app

SAND is a NOMAD **dashboard** plugin: it is listed on the (new) NOMAD GUI's
*Dashboards* page and opens in a new tab. It does not run embedded in the GUI,
whose iframe blocks downloads (the sheet). It is mounted at (**note the
trailing slash**):

```
http://localhost:8000/nomad-oasis/dashboards/sand/
```

The general form is `<api_base_path>/dashboards/sand/`, i.e. NOMAD's API base
path (`config.services.api_base_path`, default `/nomad-oasis`) with the
dashboard's `sand` id appended. Dashboards need `nomad-lab>=1.4.3`.

| Method | URL | Description |
|--------|-----|-------------|
| `GET`  | `http://localhost:8000/nomad-oasis/dashboards/sand/` | The SAND UI (`static/index.html`) |
| `GET`  | `http://localhost:8000/nomad-oasis/dashboards/sand/docs` | FastAPI Swagger / OpenAPI docs |
| `GET`  | `http://localhost:8000/nomad-oasis/dashboards/sand/api/me` | The logged-in user's name |
| `GET`  | `http://localhost:8000/nomad-oasis/dashboards/sand/api/input-collections` | The user's unpublished experiments |
| `POST` | `http://localhost:8000/nomad-oasis/dashboards/sand/api/input-collections` | Create an experiment (optionally with the info form) |
| `POST` | `http://localhost:8000/nomad-oasis/dashboards/sand/api/input-collections/{upload_id}/audio` | Add a recording (→ AudioInput entry) |
| `POST` | `http://localhost:8000/nomad-oasis/dashboards/sand/api/input-collections/{upload_id}/notes` | Add a typed step note (→ WrittenNote entry) |

SAND has no login of its own: it uses the login of the NOMAD GUI, which renews
the token in the cookie while it is open. So SAND works only while the NOMAD
GUI is open in another tab of the same browser. SAND watches the token and warns when it is about to
expire or has expired; it then does not start a new recording.
