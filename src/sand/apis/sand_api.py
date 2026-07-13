from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from nomad.app.v1.routers.auth import get_current_user
from nomad.config import config

from sand.apis.routers.extract import router as extract_router
from sand.apis.routers.pipeline import router as pipeline_router
from sand.apis.routers.transcribe import router as transcribe_router
from sand.services.extraction import ExtractionService
from sand.services.nomad_upload import NomadUploader
from sand.services.stt import GroqSTTService

# TODO: this need to be updated maybe to uplaod access when the api scope is supprted.
require_login = Depends(get_current_user({}, allow_anonymous=False))

STATIC_DIR = Path(__file__).parent / 'static'

sand_api_entry_point = config.get_plugin_entry_point('sand.apis:sand_api')

app = FastAPI(
    title='SAND',
    version='0.1.0',
    root_path=f'{config.services.api_base_path}/{sand_api_entry_point.prefix}',
)

# Read config from the entry point (configured in nomad.yaml)
app.state.stt = GroqSTTService(
    api_key=sand_api_entry_point.groq_api_key,
    model=sand_api_entry_point.whisper_model,
)
app.state.extraction = ExtractionService(
    api_key=sand_api_entry_point.anthropic_api_key,
    model=sand_api_entry_point.anthropic_model,
)
app.state.nomad = NomadUploader(
    base_url=sand_api_entry_point.nomad_base_url,
)

app.include_router(transcribe_router, prefix='/api', dependencies=[require_login])
app.include_router(extract_router, prefix='/api', dependencies=[require_login])
app.include_router(pipeline_router, prefix='/api', dependencies=[require_login])


@app.get('/auth/config')
async def auth_config():
    """Return Keycloak config so the frontend can initialize authentication."""
    return {
        'keycloak_url': config.keycloak.public_server_url,
        'keycloak_realm': config.keycloak.realm_name,
        'keycloak_client_id': config.keycloak.client_id,
    }


@app.get('/')
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / 'index.html')


app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')
