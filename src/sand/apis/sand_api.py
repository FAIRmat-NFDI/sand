import json
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from nomad.app.v1.routers.auth import get_current_user
from nomad.config import config

from sand.apis.routers.experiments import router as experiments_router
from sand.hysprint.generate import HysprintInputError
from sand.services.extraction_service import ExtractionService
from sand.services.nomad_api import NomadAPIError, NomadAuthError
from sand.services.voice_eln import VoiceElnService

# NOMAD statuses passed through to the caller as-is.
CLIENT_ERROR_STATUSES = (400, 404, 409)

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
# Transcription happens inside NOMAD (voice-eln plugin); sand only creates the
# AudioInput entry and links the user to it.
app.state.voice_eln = VoiceElnService(
    base_url=sand_api_entry_point.nomad_base_url,
)
app.state.extraction_service = ExtractionService(
    model_name=sand_api_entry_point.llm_model_name,
    api_key=sand_api_entry_point.llm_api_key,
)

app.include_router(experiments_router, prefix='/api', dependencies=[require_login])


def _nomad_detail(exc: NomadAPIError) -> str:
    """The human-readable message: NOMAD errors carry a raw JSON body."""
    try:
        body = json.loads(exc.detail)
    except ValueError:
        return exc.detail
    if isinstance(body, dict) and isinstance(body.get('detail'), str):
        return body['detail']
    return exc.detail


@app.exception_handler(NomadAPIError)
async def nomad_api_error_handler(request: Request, exc: NomadAPIError):
    """Translate NOMAD API failures instead of try/except in every endpoint."""
    if isinstance(exc, NomadAuthError):
        return JSONResponse(status_code=401, content={'detail': _nomad_detail(exc)})
    if exc.status_code in CLIENT_ERROR_STATUSES:
        return JSONResponse(
            status_code=exc.status_code, content={'detail': _nomad_detail(exc)}
        )
    return JSONResponse(status_code=502, content={'detail': str(exc)})


@app.exception_handler(HysprintInputError)
async def hysprint_input_error_handler(request: Request, exc: HysprintInputError):
    return JSONResponse(status_code=400, content={'detail': str(exc)})


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
