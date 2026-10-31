"""POST /input-collections/from-sheet against a fake NOMAD."""

import contextlib
from dataclasses import dataclass, field
from http import HTTPStatus

import httpx
import pytest
from fastapi import FastAPI
from synthetic_sheet import edited

from sand.apis.routers.input_collections import router
from sand.services.nomad_api import XLSX_CONTENT_TYPE
from sand.services.voice_eln import EntryHandle
from sand.sheet_edit import catalog
from sand.sheet_edit.session import ORIGINAL_FILE
from sand.sheet_edit.workbook import sha256

pytestmark = pytest.mark.asyncio

UPLOAD = 'upload-1'
ENTRY = 'entry-1'


@dataclass
class FakeWriter:
    files: dict[str, bytes] = field(default_factory=dict)

    async def upload_raw_file(self, client, upload_id, name, content, content_type):
        assert upload_id == UPLOAD
        assert content_type == XLSX_CONTENT_TYPE
        self.files[name] = content


@dataclass
class FakeVoice:
    writer: FakeWriter = field(default_factory=FakeWriter)
    created: list[str] = field(default_factory=list)

    def build_client(self, token):
        assert token == 'tok'
        return contextlib.nullcontext(object())

    async def create_input_collection(self, client, name):
        self.created.append(name)
        return EntryHandle(upload_id=UPLOAD, entry_id=ENTRY)

    def entry_url(self, upload_id, entry_id):
        return f'http://nomad/{upload_id}/{entry_id}'


@pytest.fixture
def voice() -> FakeVoice:
    return FakeVoice()


def api(voice: FakeVoice, token: str | None = 'tok') -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router, prefix='/api')
    app.state.voice_eln = voice
    headers = {} if token is None else {'Authorization': f'Bearer {token}'}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='http://sand', headers=headers
    )


async def post_sheet(voice, xlsx: bytes, filename='batch.xlsx', token='tok', **form):
    async with api(voice, token) as client:
        return await client.post(
            '/api/input-collections/from-sheet',
            files={'file': (filename, xlsx, XLSX_CONTENT_TYPE)},
            data=form,
        )


async def test_good_sheet_becomes_a_collection(voice, synthetic_xlsx):
    response = await post_sheet(voice, synthetic_xlsx, name=' batch 7 ')

    assert response.status_code == HTTPStatus.CREATED, response.text
    body = response.json()
    assert body['upload_id'] == UPLOAD
    assert body['entry_id'] == ENTRY
    assert body['entry_url'] == f'http://nomad/{UPLOAD}/{ENTRY}'
    assert body['name'] == 'batch 7'
    assert voice.created == ['batch 7']
    # the bytes are stored as they came: the job verifies against them
    assert voice.writer.files == {ORIGINAL_FILE: synthetic_xlsx}

    # the summary is the catalog the job will edit against
    built = catalog.build(synthetic_xlsx)
    sheet = body['sheet']
    assert sheet['name'] == built.structure.sheet_name
    assert sheet['sha256'] == sha256(synthetic_xlsx)
    assert sheet['samples'] == [s.label for s in built.samples]
    assert sheet['samples'][:3] == ['1', '2', '3']
    assert [(c['id'], c['section'], c['header']) for c in sheet['columns']] == [
        (c.id, c.column.section.label, c.column.header) for c in built.columns
    ]
    assert ('2: Spin Coating', 'Rotation speed [rpm]') in {
        (c['section'], c['header']) for c in sheet['columns']
    }


async def test_real_kit_sheet_is_accepted(voice, real_kit_xlsx):
    # a round trip through openpyxl would lose its formula results; the
    # writer never saves that way, so only the ingest checks decide
    response = await post_sheet(voice, real_kit_xlsx)

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert voice.writer.files == {ORIGINAL_FILE: real_kit_xlsx}


async def test_name_defaults_to_the_file_name(voice, synthetic_xlsx):
    response = await post_sheet(voice, synthetic_xlsx, filename='Batch 7.xlsx')

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert response.json()['name'] == 'Batch 7'
    assert voice.created == ['Batch 7']


async def test_sheet_with_problems_is_refused_before_creating(voice, synthetic_xlsx):
    def rename_info(ws):
        ws['A1'] = 'Info'

    response = await post_sheet(voice, edited(synthetic_xlsx, rename_info))

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json() == {
        'detail': {
            'problems': [
                "cell A1 must be 'Experiment Info' (found 'Info')",
                "'Experiment Info' has no 'Nomad ID' column",
                "'Experiment Info' has no 'Sample' column",
            ]
        }
    }
    assert voice.created == []
    assert voice.writer.files == {}


async def test_not_a_spreadsheet(voice):
    response = await post_sheet(voice, b'not a spreadsheet')

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    [problem] = response.json()['detail']['problems']
    assert problem.startswith('not a readable .xlsx file')
    assert voice.created == []


async def test_empty_file(voice):
    response = await post_sheet(voice, b'')

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert voice.created == []


async def test_not_logged_in(voice, synthetic_xlsx):
    response = await post_sheet(voice, synthetic_xlsx, token=None)

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert voice.created == []
