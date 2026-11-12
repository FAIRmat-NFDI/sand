"""POST /input-collections/{upload}/feedback against a fake NOMAD."""

import json
from http import HTTPStatus

import pytest
from nomad_fakes import COLLECTION, UPLOAD, USERNAME, FakeVoice, api, nomad_down, note

from sand.feedback import FEEDBACK_DIR
from sand.sheet_edit.session import CONFIRMATIONS_FILE, STATUS_FILE

pytestmark = pytest.mark.asyncio


@pytest.fixture
def voice() -> FakeVoice:
    return FakeVoice(inputs=[note('n1', 'speed is 4000 for samples 1 to 3')])


async def send(voice, **body):
    async with api(voice) as client:
        return await client.post(
            f'/api/input-collections/{UPLOAD}/feedback',
            params={'collection_entry_id': COLLECTION},
            json={'what_wrong': 'sample 3 kept the old speed', **body},
        )


def stored(voice, response) -> dict:
    return json.loads(voice.writer.files[response.json()['file']])


async def test_a_report_is_stored_beside_the_sheet_with_what_the_user_saw(voice):
    status = {'phase': 'completed', 'collection_entry_id': COLLECTION}
    voice.writer.files[STATUS_FILE] = json.dumps(status).encode()
    voice.writer.files[CONFIRMATIONS_FILE] = json.dumps({'k1': {}}).encode()

    response = await send(
        voice, expected='4000 for all three', recent_errors=['10:15:02 GET x 502']
    )

    assert response.status_code == HTTPStatus.CREATED, response.text
    file = response.json()['file']
    assert file.startswith(FEEDBACK_DIR + '/report_')
    report = stored(voice, response)
    assert report['what_wrong'] == 'sample 3 kept the old speed'
    assert report['expected'] == '4000 for all three'
    assert report['by'] == USERNAME
    assert report['status'] == 'new'
    assert report['sheet'] == {
        'upload_id': UPLOAD,
        'collection_entry_id': COLLECTION,
        'name': 'batch 7',
    }
    assert [i['text'] for i in report['context']['inputs']] == [
        'speed is 4000 for samples 1 to 3'
    ]
    assert report['context']['edit_status'] == status
    assert report['context']['confirmations'] == {'k1': {}}
    assert report['context']['error'] is None
    assert report['client']['recent_errors'] == ['10:15:02 GET x 502']


async def test_a_report_is_kept_when_the_sheet_cannot_be_read(voice):
    async def broken(*args):
        raise nomad_down('collect_inputs')

    voice.collect_inputs = broken

    response = await send(voice)

    assert response.status_code == HTTPStatus.CREATED, response.text
    report = stored(voice, response)
    assert report['what_wrong'] == 'sample 3 kept the old speed'
    assert 'NOMAD is down' in report['context']['error']
    assert report['context']['inputs'] == []


async def test_another_collections_edit_status_is_left_out(voice):
    voice.writer.files[STATUS_FILE] = json.dumps(
        {'phase': 'completed', 'collection_entry_id': 'collection-2'}
    ).encode()

    response = await send(voice)

    assert stored(voice, response)['context']['edit_status'] is None


async def test_two_reports_do_not_overwrite_each_other(voice):
    first = await send(voice)
    second = await send(voice, what_wrong='and again')

    assert first.json()['file'] != second.json()['file']


async def test_an_empty_report_is_refused(voice):
    response = await send(voice, what_wrong='')

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert voice.writer.files == {}


async def test_an_oversized_error_line_is_refused(voice):
    response = await send(voice, recent_errors=['x' * 1001])

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert voice.writer.files == {}


async def test_a_failed_write_is_reported(voice):
    voice.writer.failing = True

    response = await send(voice)

    assert response.status_code == HTTPStatus.BAD_GATEWAY
