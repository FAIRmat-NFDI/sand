"""A fake NOMAD for the endpoint tests: the voice service over an
in-memory raw-file store, and the app around the router."""

import contextlib
import json
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI

from sand.apis.routers.input_collections import router
from sand.services.nomad_api import NomadAPIError
from sand.services.voice_eln import CollectedInput, EntryHandle

UPLOAD = 'upload-1'
COLLECTION = 'collection-1'
USER = 'user-1'
USERNAME = 'tester'
TOKEN = 'tok'


def nomad_down(step: str) -> NomadAPIError:
    return NomadAPIError(503, 'NOMAD is down', step=step)


def nomad(me: dict):
    """What the endpoints ask NOMAD directly, beside the voice service."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == '/users/me'
        return httpx.Response(200, json=me)

    return handler


@dataclass
class FakeWriter:
    files: dict[str, bytes] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)
    failing: bool = False  # every write raises
    writes_allowed: int | None = None  # None: all; else writes before NOMAD is down

    async def read_raw_file(self, client, upload_id, name):
        return self.files.get(name)

    async def read_json(self, client, upload_id, name):
        raw = self.files.get(name)
        return None if raw is None else json.loads(raw)

    async def upload_raw_file(self, client, upload_id, name, content, content_type):
        assert upload_id == UPLOAD
        if self.failing or self.writes_allowed == 0:
            raise nomad_down('upload_raw_file')
        if self.writes_allowed is not None:
            self.writes_allowed -= 1
        self.files[name] = content
        self.types[name] = content_type

    async def write_json(self, client, upload_id, name, payload):
        await self.upload_raw_file(
            client, upload_id, name, json.dumps(payload).encode(), 'application/json'
        )


@dataclass
class FakeVoice:
    writer: FakeWriter = field(default_factory=FakeWriter)
    inputs: list[CollectedInput] = field(default_factory=list)
    name: str = 'batch 7'
    me: dict = field(default_factory=lambda: {'user_id': USER, 'username': USERNAME})
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)
    undeletable: bool = False

    def build_client(self, token):
        assert token == TOKEN
        return contextlib.nullcontext(
            httpx.AsyncClient(
                transport=httpx.MockTransport(nomad(self.me)), base_url='http://n'
            )
        )

    def entry_url(self, upload_id, entry_id):
        return f'http://nomad/{upload_id}/{entry_id}'

    async def create_input_collection(self, client, name):
        self.created.append(name)
        return EntryHandle(upload_id=UPLOAD, entry_id=COLLECTION)

    async def delete_input_collection(self, client, upload_id):
        if self.undeletable:
            raise nomad_down('delete_upload')
        self.deleted.append(upload_id)

    async def collect_inputs(self, client, upload_id, collection_entry_id):
        assert (upload_id, collection_entry_id) == (UPLOAD, COLLECTION)
        return self.inputs

    async def revise_input_datetime(
        self, client, upload_id, entry_id, datetime_value, collection_entry_id
    ):
        self.moved.append((entry_id, datetime_value))
        return 'note'

    async def input_collection_name(self, client, upload_id, collection_entry_id):
        assert (upload_id, collection_entry_id) == (UPLOAD, COLLECTION)
        return self.name


def note(entry_id: str, text: str | None) -> CollectedInput:
    return CollectedInput(
        entry_id=entry_id, kind='note', text=text, label='', datetime=None
    )


def api(voice: FakeVoice, token: str | None = TOKEN) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router, prefix='/api')
    app.state.voice_eln = voice
    headers = {} if token is None else {'Authorization': f'Bearer {token}'}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='http://sand', headers=headers
    )
