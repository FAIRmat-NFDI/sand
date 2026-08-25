# sand

Voice/text AI assistant for extracting structured lab process data into NOMAD.

## How it works

```
create an experiment in the SAND UI (with the experiment-info form)
  -> one NOMAD upload with an InputCollection entry
     (+ a WrittenNote labeled 'experiment_info' holding the form JSON)

record audio / save a step note in the SAND UI (experiment selected)
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

SAND is not a standalone application — it is a **NOMAD plugin**. It is mounted onto NOMAD's API server under the `sand/` prefix. To run it you
start a NOMAD instance with this plugin installed and configured. The easiest way
to do this for development is via the
[`nomad-distro-dev`](https://github.com/FAIRmat-NFDI/nomad-distro-dev) repository.

### Prerequisites

Before you can run the SAND app you need a few things in place:

- A working [`nomad-distro-dev`](https://github.com/FAIRmat-NFDI/nomad-distro-dev)
  checkout with its [basic infra prerequisites](https://github.com/FAIRmat-NFDI/nomad-distro-dev#basic-infra)
- The [`nomad-voice-eln`](https://github.com/FAIRmat-NFDI/nomad-voice-eln) plugin
  **installed and enabled in the same NOMAD** — it owns audio entries and
  transcription. Follow its README for setup, including `GROQ_API_KEY` in the
  **action worker's environment** (speech-to-text runs there, not in SAND).
- An **Anthropic API key** — used for the AI extraction of structured data. Get
  one from <https://console.anthropic.com/>.

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
**enable** the SAND API entry point and **provide your API keys**, otherwise the
app will load but the AI features will not work:

```yaml
plugins:
  entry_points:
    include:
      - sand.apis:sand_api
      # plus the voice-eln entry points, see the nomad-voice-eln README
    options:
      sand.apis:sand_api:
        anthropic_api_key: '<your-anthropic-api-key>'  # required: AI extraction
        anthropic_model: 'claude-sonnet-4-20250514'    # Anthropic model
        # Base URL of the NOMAD API the app uploads to. For a local instance:
        nomad_base_url: 'http://localhost:8000/nomad-oasis/api/v1'
```

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

uv run poe gui start
```

### 4. Open the app

With the default `/nomad-oasis/api` base path, the SAND app is available at:

The app is mounted at the base URL (**note the trailing slash**):

```
http://localhost:8000/nomad-oasis/sand/
```

The general form is `<api_base_path>/sand/`, i.e. NOMAD's API base path
(`config.services.api_base_path`, default `/nomad-oasis`) with the plugin's `sand`
prefix appended.

| Method | URL | Description |
|--------|-----|-------------|
| `GET`  | `http://localhost:8000/nomad-oasis/sand/` | The SAND UI (`static/index.html`) |
| `GET`  | `http://localhost:8000/nomad-oasis/sand/docs` | FastAPI Swagger / OpenAPI docs |
| `GET`  | `http://localhost:8000/nomad-oasis/sand/auth/config` | Keycloak config for the frontend |
| `GET`  | `http://localhost:8000/nomad-oasis/sand/api/input-collections` | The user's unpublished experiments |
| `POST` | `http://localhost:8000/nomad-oasis/sand/api/input-collections` | Create an experiment (optionally with the info form) |
| `POST` | `http://localhost:8000/nomad-oasis/sand/api/input-collections/{upload_id}/audio` | Add a recording (→ AudioInput entry) |
| `POST` | `http://localhost:8000/nomad-oasis/sand/api/input-collections/{upload_id}/notes` | Add a typed step note (→ WrittenNote entry) |
| `POST` | `http://localhost:8000/nomad-oasis/sand/api/extract` | AI extraction (text → structured data) |
| `POST` | `http://localhost:8000/nomad-oasis/sand/api/pipeline` | Full pipeline (text → structured data → NOMAD upload) |

The `input-collections`, `extract`, and `pipeline` routes require a logged-in
user, so you must authenticate through Keycloak before calling them.
