import os
from http import HTTPStatus
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from nomad.app.v1.routers.auth import get_current_user
from nomad.config import config

from sand.apis.deps import get_bearer_token
from sand.apis.routers.input_collections import router as input_collections_router
from sand.apis.routers.live_transcript import router as live_transcript_router
from sand.services.voice_eln import VoiceElnService

# TODO: this need to be updated maybe to uplaod access when the api scope is supprted.
require_login = Depends(get_current_user({}, allow_anonymous=False))

STATIC_DIR = Path(__file__).parent / 'static'
# Browsers revalidate the UI files on every load (a cheap 304 when
# unchanged), so edits show up without versioned URLs - which ES module
# imports could not carry anyway.
NO_CACHE = {'Cache-Control': 'no-cache'}
# The new NOMAD GUI (the nomad-gui plugin's mount): sand has no login of its
# own, the user logs in there and the GUI sets the Authorization cookie.
NOMAD_GUI_URL = f'{config.services.api_base_path.rstrip("/")}/gui/v2/'


class RevalidatedStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_CACHE)
        return response


sand_api_entry_point = config.get_plugin_entry_point('sand.apis:sand_api')

app = FastAPI(
    title='SAND',
    version='0.1.0',
    root_path=f'{config.services.api_base_path}/dashboards/{sand_api_entry_point.id_url_safe}',
)

# Read config from the entry point (configured in nomad.yaml)
# Transcription happens inside NOMAD (voice-eln plugin); sand only creates the
# AudioInput entry and links the user to it.
app.state.voice_eln = VoiceElnService(
    base_url=sand_api_entry_point.nomad_base_url,
)
app.state.deepgram_api_key = sand_api_entry_point.deepgram_api_key or os.environ.get(
    'DEEPGRAM_API_KEY', ''
)
app.state.deepgram_model = sand_api_entry_point.deepgram_model
app.state.store_live_transcript = sand_api_entry_point.store_live_transcript

app.include_router(
    input_collections_router, prefix='/api', dependencies=[require_login]
)
# No require_login here: NOMAD's dependency needs an HTTP request, so the
# socket checks the cookie of its handshake itself.
app.include_router(live_transcript_router, prefix='/api')


@app.get('/ui-config')
async def ui_config():
    """Frontend defaults: whether live transcription exists at all, the
    default state of the save-live-transcript toggle, and where to log in."""
    return {
        'nomad_gui_url': NOMAD_GUI_URL,
        'live_transcript_available': bool(app.state.deepgram_api_key),
        'store_live_transcript': app.state.store_live_transcript,
    }


@app.get('/api/me')
async def me(request: Request) -> dict:
    """The logged-in user's display name; 401 tells the UI to show its
    login prompt."""
    token = get_bearer_token(request)
    async with app.state.voice_eln.build_client(token) as client:
        response = await client.get('/users/me')
    if response.status_code != HTTPStatus.OK:
        raise HTTPException(status_code=401, detail='Not logged in to NOMAD')
    user = response.json()
    return {'name': user.get('username') or user.get('name') or ''}


@app.get('/')
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / 'index.html', headers=NO_CACHE)


app.mount('/static', RevalidatedStaticFiles(directory=STATIC_DIR), name='static')
