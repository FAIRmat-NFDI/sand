"""A programmable fake of the NOMAD API, shared by the service tests."""

import json

import httpx
from nomad.utils import generate_entry_id

from sand.services.nomad_api import entry_ref
from sand.services.voice_eln import EXPERIMENT_MAINFILE, VoiceElnService

BASE_URL = 'http://localhost:8000/nomad-oasis/api/v1'
UPLOAD_ID = 'up-123'
SAND_COLLECTION_ID = generate_entry_id(UPLOAD_ID, EXPERIMENT_MAINFILE)


def voice_service() -> VoiceElnService:
    # retry_interval_s=0: no sleeps in tests
    return VoiceElnService(BASE_URL, retry_interval_s=0, write_timeout_s=5)


def fake_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_URL)


class FakeNomad:
    """Programmable NOMAD API: upload status, raw files, and entry queries.

    `entries` maps entry ids to mainfiles: an entry-id query finds its
    mainfile and the entry's archive is that raw file, as if processed.
    `processed` maps an entry id to the entry ids its parse created (its
    processed_archive), for entries whose raw file is not JSON (a sheet).

    `processing_polls` makes that many status GETs report process_running
    before the upload goes idle. `blocked_writes` rejects that many PUTs
    with NOMAD's processing-lock error (the check-then-PUT race).
    """

    def __init__(
        self,
        query_results=None,
        processing_polls=0,
        blocked_writes=0,
        published=False,
        upload_exists=True,
    ):
        self.raw_files: dict[str, bytes] = {}
        self.entries: dict[str, str] = {}
        self.processed: dict[str, list[str]] = {}
        self.query_results = query_results or []
        self.processing_polls = processing_polls
        self.blocked_writes = blocked_writes
        self.published = published
        self.upload_exists = upload_exists
        self.put_attempts = 0

    def _status(self) -> httpx.Response:
        if not self.upload_exists:
            return httpx.Response(404, json={'detail': 'upload not found'})
        running = self.processing_polls > 0
        if running:
            self.processing_polls -= 1
        return httpx.Response(
            200,
            json={'data': {'process_running': running, 'published': self.published}},
        )

    def _put_raw(self, request: httpx.Request) -> httpx.Response:
        self.put_attempts += 1
        if self.blocked_writes > 0:
            self.blocked_writes -= 1
            # after a blocked PUT the service re-checks the status; report
            # processing once so it retries instead of failing
            self.processing_polls = max(self.processing_polls, 1)
            return httpx.Response(
                400,
                json={'detail': 'The upload is currently blocked by another process.'},
            )
        name = request.url.params['file_name']
        if '/' in name:
            return httpx.Response(400, json={'detail': 'Bad file name provided.'})
        directory = request.url.path.split('/raw/', 1)[1].strip('/')
        full_name = f'{directory}/{name}' if directory else name
        self.raw_files[full_name] = request.content
        return httpx.Response(200, json={})

    def _raw(self, request: httpx.Request) -> httpx.Response:
        if request.method == 'PUT':
            return self._put_raw(request)
        name = request.url.path.split('/raw/', 1)[1]
        if request.method == 'DELETE':
            if self.raw_files.pop(name, None) is None:
                return httpx.Response(404, json={'detail': 'not found'})
            return httpx.Response(200, json={})
        if name not in self.raw_files:
            return httpx.Response(404, json={'detail': 'not found'})
        return httpx.Response(200, content=self.raw_files[name])

    def _entries(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == 'POST' and path.endswith('/entries/query'):
            query = json.loads(request.content)['query']
            if 'entry_id:any' in query:
                data = [
                    {'entry_id': eid, 'mainfile': self.entries[eid]}
                    for eid in query['entry_id:any']
                    if eid in self.entries
                ]
                return httpx.Response(200, json={'data': data})
            entry_id = query.get('entry_id')
            if not (self.entries and entry_id):
                return httpx.Response(200, json={'data': self.query_results})
            found = self.entries.get(entry_id)
            data = [{'mainfile': found}] if found else []
            return httpx.Response(200, json={'data': data})
        entry_id = path.split('/entries/')[1].split('/')[0]
        if request.method == 'GET' and entry_id in self.processed:
            refs = [entry_ref(UPLOAD_ID, eid) for eid in self.processed[entry_id]]
            archive = {'data': {'processed_archive': refs}}
            return httpx.Response(200, json={'data': {'archive': archive}})
        mainfile = self.entries.get(entry_id)
        if request.method != 'GET' or mainfile not in self.raw_files:
            return httpx.Response(404, json={'detail': f'unexpected {path}'})
        archive = json.loads(self.raw_files[mainfile])
        return httpx.Response(200, json={'data': {'archive': archive}})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == 'POST' and path.endswith('/uploads'):
            return httpx.Response(200, json={'upload_id': UPLOAD_ID})
        if f'/uploads/{UPLOAD_ID}/raw/' in path:
            return self._raw(request)
        if request.method == 'GET' and '/uploads/' in path:
            if path.endswith('/uploads/' + UPLOAD_ID):
                return self._status()
            return httpx.Response(404, json={'detail': 'upload not found'})
        if '/entries/' in path:
            return self._entries(request)
        return httpx.Response(404, json={'detail': f'unexpected {path}'})

    def archive(self, mainfile: str) -> dict:
        return json.loads(self.raw_files[mainfile])
