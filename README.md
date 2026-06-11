# sand-app


## Running the SAND app

`sand-app` is not a standalone application — it is a **NOMAD plugin**. It is mounted onto NOMAD's API server under the `sand/` prefix. To run it you
start a NOMAD instance with this plugin installed and configured. The easiest way
to do this for development is via the
[`nomad-distro-dev`](https://github.com/FAIRmat-NFDI/nomad-distro-dev) repository.

### Prerequisites

Before you can run the SAND app you need a few things in place:

- A working [`nomad-distro-dev`](https://github.com/FAIRmat-NFDI/nomad-distro-dev)
  checkout with its [basic infra prerequisites](https://github.com/FAIRmat-NFDI/nomad-distro-dev#basic-infra)

- A **Groq API key** — used for speech-to-text (Whisper). Get one from
  <https://console.groq.com/keys>.
- 
- An **Anthropic API key** — used for the AI extraction of structured data. Get
  one from <https://console.anthropic.com/>.

### 1. Add the plugin to a NOMAD dev distribution

The SAND app is loaded as part of a NOMAD distribution, so the plugin first has
to live inside your `nomad-distro-dev` checkout as a workspace package. From the
root of `nomad-distro-dev`, add it under `packages/` (as a git submodule if you
have a repo for it) and register it with `uv`:

```sh
# Add the plugin source under packages/ (submodule shown here; a plain copy works too)
git submodule add https://github.com/FAIRmat-NFDI/sand-app.git packages/sand-app

# Register it as an editable workspace dependency
uv add packages/sand-app
```

This adds `sand-app` to `[project.dependencies]` and `[tool.uv.sources]` in the
distribution's `pyproject.toml` (with `sand-app = { workspace = true }`).

### 2. Configure the plugin in `nomad.yaml`

The `uv run poe setup` step (below) creates a `nomad.yaml` in the root of your
`nomad-distro-dev` checkout if one does not exist yet. You must edit it to
**enable** the SAND API entry point and **provide your API keys**, otherwise the
app will load but the AI features will not work:

```yaml
plugins:
  entry_points:
    include:
      - sand_app.apis:sand_api
    options:
      sand_app.apis:sand_api:
        groq_api_key: '<your-groq-api-key>'        # required: speech-to-text
        whisper_model: 'whisper-large-v3-turbo'    # Groq Whisper model
        anthropic_api_key: '<your-anthropic-api-key>'  # required: AI extraction
        anthropic_model: 'claude-sonnet-4-20250514'    # Anthropic model
        # Base URL of the NOMAD API the app uploads to. For a local instance:
        nomad_base_url: 'http://localhost:8000/nomad-oasis/api/v1'
```


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
| `POST` | `http://localhost:8000/nomad-oasis/sand/api/transcribe` | Speech-to-text (audio → text) |
| `POST` | `http://localhost:8000/nomad-oasis/sand/api/extract` | AI extraction (text → structured data) |
| `POST` | `http://localhost:8000/nomad-oasis/sand/api/pipeline` | Full pipeline (audio → structured data → NOMAD upload) |

The `transcribe`, `extract`, and `pipeline` routes require a logged-in user, so
you must authenticate through Keycloak before calling them.
