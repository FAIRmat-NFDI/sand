"""sand's own files skip NOMAD's processing; several go in one request."""

import httpx
import pytest

from sand.services.nomad_api import RawFileWriter

BASE_URL = 'http://nomad.test/api/v1'
UPLOAD = 'u1'

pytestmark = pytest.mark.asyncio


class Recorder:
    def __init__(self):
        self.writes: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == 'GET':
            return httpx.Response(200, json={'data': {'process_running': False}})
        self.writes.append(request)
        return httpx.Response(200, json={})


async def written(*calls) -> list[httpx.Request]:
    recorder = Recorder()
    writer = RawFileWriter(retry_interval_s=0, write_timeout_s=5)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(recorder), base_url=BASE_URL
    ) as client:
        for call in calls:
            await call(writer, client)
    return recorder.writes


async def test_a_sheet_edit_file_is_stored_without_processing():
    (put,) = await written(
        lambda w, c: w.write_json(c, UPLOAD, 'sheet_edit/status.json', {})
    )

    assert put.url.path == f'/api/v1/uploads/{UPLOAD}/raw/sheet_edit'
    assert put.url.params['trigger_processing'] == 'false'


async def test_any_other_file_is_processed():
    # a note or the collection: NOMAD makes its entry from it
    (put,) = await written(
        lambda w, c: w.write_json(c, UPLOAD, 'note.archive.json', {})
    )

    assert 'trigger_processing' not in put.url.params


async def test_several_files_go_into_a_directory_in_one_request():
    files = [('a.json', b'{}', 'application/json'), ('b.xlsx', b'x', 'application/x')]

    (put,) = await written(
        lambda w, c: w.upload_raw_files(c, UPLOAD, files, directory='sheet_edit')
    )

    assert put.url.path == f'/api/v1/uploads/{UPLOAD}/raw/sheet_edit'
    assert put.url.params['trigger_processing'] == 'false'
    assert b'filename="a.json"' in put.content
    assert b'filename="b.xlsx"' in put.content


async def test_a_name_with_a_directory_is_refused():
    with pytest.raises(ValueError, match='within `directory`'):
        await written(
            lambda w, c: w.upload_raw_files(
                c, UPLOAD, [('x/a.json', b'{}', 'application/json')]
            )
        )


async def test_a_sheet_edit_file_is_deleted_without_processing():
    (delete,) = await written(
        lambda w, c: w.delete_raw_file(c, UPLOAD, 'sheet_edit/llm_cache.x.json')
    )

    assert delete.method == 'DELETE'
    assert delete.url.params['trigger_processing'] == 'false'


async def test_a_directory_lists_its_files():
    listing = {
        'path': 'sheet_edit',
        'access': 'unpublished',
        'directory_metadata': {
            'name': 'sheet_edit',
            'content': [
                {'name': 'status.json', 'is_file': True},
                {'name': 'old', 'is_file': False},
            ],
        },
    }

    def nomad(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f'/api/v1/uploads/{UPLOAD}/rawdir/sheet_edit'
        return httpx.Response(200, json=listing)

    writer = RawFileWriter(retry_interval_s=0, write_timeout_s=5)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(nomad), base_url=BASE_URL
    ) as client:
        assert await writer.list_raw_dir(client, UPLOAD, 'sheet_edit') == [
            'status.json'
        ]


async def test_a_missing_directory_lists_nothing():
    writer = RawFileWriter(retry_interval_s=0, write_timeout_s=5)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        base_url=BASE_URL,
    ) as client:
        assert await writer.list_raw_dir(client, UPLOAD, 'sheet_edit') == []
