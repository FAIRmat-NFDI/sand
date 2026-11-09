"""POST /input-collections/from-sheet against a fake NOMAD."""

from http import HTTPStatus

import pytest
from nomad_fakes import COLLECTION, FakeVoice, api
from synthetic_sheet import edited

from sand.services.nomad_api import XLSX_CONTENT_TYPE
from sand.services.voice_eln import InputCollectionSummary
from sand.sheet_edit.session import ORIGINAL_FILE

pytestmark = pytest.mark.asyncio

UPLOAD = 'upload-1'


@pytest.fixture
def voice() -> FakeVoice:
    return FakeVoice()


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
    assert body['entry_id'] == COLLECTION
    assert body['entry_url'] == f'http://nomad/{UPLOAD}/{COLLECTION}'
    assert body['name'] == 'batch 7'
    assert body['created'] is not None  # the list's "uploaded 2 Nov"
    assert voice.created == ['batch 7']
    # the file's name: the original's download name
    assert voice.descriptions == ['batch.xlsx']
    # the bytes are stored as they came: the job verifies against them
    assert voice.writer.files == {ORIGINAL_FILE: synthetic_xlsx}
    assert voice.writer.types == {ORIGINAL_FILE: XLSX_CONTENT_TYPE}


async def test_the_list_says_when_each_sheet_was_uploaded(voice):
    voice.collections = [
        InputCollectionSummary(UPLOAD, 'e-1', 'Batch 7', '2026-11-02T09:30:00+00:00'),
        InputCollectionSummary('upload-0', 'e-0', 'older', None),  # made before
    ]

    async with api(voice) as client:
        response = await client.get('/api/input-collections')

    assert response.status_code == HTTPStatus.OK, response.text
    assert [
        (c['name'], c['created']) for c in response.json()['input_collections']
    ] == [
        ('Batch 7', '2026-11-02T09:30:00+00:00'),
        ('older', None),
    ]


async def test_real_kit_sheet_is_accepted(voice, real_kit_xlsx):
    # a round trip through openpyxl would lose its formula results; the
    # writer never saves that way, so only the ingest checks decide
    response = await post_sheet(voice, real_kit_xlsx)

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert voice.writer.files == {ORIGINAL_FILE: real_kit_xlsx}


async def test_nomad_failing_after_creation_takes_the_collection_back(
    voice, synthetic_xlsx
):
    voice.writer.failing = True

    response = await post_sheet(voice, synthetic_xlsx)

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert voice.created == ['batch']
    assert voice.deleted == [UPLOAD]


async def test_a_collection_that_cannot_be_taken_back_is_named(voice, synthetic_xlsx):
    voice.writer.failing = True
    voice.undeletable = True

    response = await post_sheet(voice, synthetic_xlsx)

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()['detail'] == (
        f'NOMAD is down; the half-made sheet {UPLOAD} could not be removed either'
    )


async def test_name_defaults_to_the_file_name(voice, synthetic_xlsx):
    response = await post_sheet(voice, synthetic_xlsx, filename='Batch 7.xlsx')

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert response.json()['name'] == 'Batch 7'
    assert voice.created == ['Batch 7']
    assert voice.descriptions == ['Batch 7.xlsx']


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
