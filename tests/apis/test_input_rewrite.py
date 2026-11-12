"""A written update can be revised or deleted: its confirmations go,
the next job reads it and the updates after it again."""

import json
from http import HTTPStatus

import pytest
from nomad_fakes import COLLECTION, UPLOAD, FakeVoice, api, note

from sand.sheet_edit.session import CONFIRMATIONS_FILE, STATUS_FILE

pytestmark = pytest.mark.asyncio

READY = [{'key': 'k1', 'line': '...'}, {'key': 'k2', 'line': '...'}]


def put_status(voice, reports, collection=COLLECTION):
    voice.writer.files[STATUS_FILE] = json.dumps(
        {
            'job_id': 'job-0',
            'phase': 'completed',
            'collection_entry_id': collection,
            'updated_at': '2026-10-28T12:16:05+00:00',
            'inputs': reports,
        }
    ).encode()


def put_confirmations(voice, records):
    """records: key -> input entry id, None for a record without one"""
    voice.writer.files[CONFIRMATIONS_FILE] = json.dumps(
        {
            key: {
                'by': 'user',
                'at': '2026-10-28T12:16:05+00:00',
                **({'input': entry_id} if entry_id is not None else {}),
            }
            for key, entry_id in records.items()
        }
    ).encode()


def confirmations(voice):
    return json.loads(voice.writer.files[CONFIRMATIONS_FILE])


async def revise(voice, entry_id='n1'):
    async with api(voice) as client:
        return await client.post(
            f'/api/input-collections/{UPLOAD}/inputs/{entry_id}/text',
            params={'collection_entry_id': COLLECTION},
            json={'text': 'time 1 is 1100'},
        )


async def delete(voice, entry_id='n1'):
    async with api(voice) as client:
        return await client.delete(
            f'/api/input-collections/{UPLOAD}/inputs/{entry_id}',
            params={'collection_entry_id': COLLECTION},
        )


async def status(voice):
    async with api(voice) as client:
        response = await client.get(
            f'/api/input-collections/{UPLOAD}/edit-status',
            params={'collection_entry_id': COLLECTION},
        )
    assert response.status_code == HTTPStatus.OK
    return response.json()


@pytest.fixture
def voice() -> FakeVoice:
    voice = FakeVoice()
    put_status(voice, [{'input': 1, 'entry_id': 'n1', 'ready': READY, 'confirmed': []}])
    return voice


async def test_a_written_update_can_be_revised_and_loses_its_confirmations(voice):
    put_confirmations(voice, {'k1': 'n1', 'k9': 'n2'})

    response = await revise(voice)

    assert response.status_code == HTTPStatus.OK
    assert voice.revised == [('n1', 'time 1 is 1100')]
    assert list(confirmations(voice)) == ['k9']


async def test_a_written_update_can_be_deleted_and_loses_its_confirmations(voice):
    put_confirmations(voice, {'k1': 'n1', 'k9': 'n2'})

    response = await delete(voice)

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert voice.removed == ['n1']
    assert list(confirmations(voice)) == ['k9']


async def test_a_record_without_its_input_goes_by_the_last_report(voice):
    put_confirmations(voice, {'k1': None, 'k9': None})

    assert (await revise(voice)).status_code == HTTPStatus.OK
    assert list(confirmations(voice)) == ['k9']


async def test_another_collections_report_names_no_old_record():
    voice = FakeVoice()
    put_status(
        voice,
        [{'input': 1, 'entry_id': 'n1', 'ready': READY, 'confirmed': []}],
        collection='collection-2',
    )
    put_confirmations(voice, {'k1': None})

    assert (await revise(voice)).status_code == HTTPStatus.OK
    assert list(confirmations(voice)) == ['k1']


async def test_an_unconfirmed_update_leaves_the_file_alone(voice):
    assert (await revise(voice)).status_code == HTTPStatus.OK
    assert CONFIRMATIONS_FILE not in voice.writer.files


async def test_a_damaged_confirmations_file_is_not_rewritten(voice):
    voice.writer.files[CONFIRMATIONS_FILE] = b'not json'

    response = await revise(voice)

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'].startswith('confirmations file damaged')
    assert voice.writer.files[CONFIRMATIONS_FILE] == b'not json'


async def test_an_update_after_a_changed_one_is_to_confirm_again(voice):
    # n2 was confirmed as k5; n1 was revised since, so the sheet before n2
    # changed and its edit has a new key
    put_status(
        voice,
        [
            {'input': 1, 'entry_id': 'n1', 'ready': READY, 'confirmed': []},
            {
                'input': 2,
                'entry_id': 'n2',
                'ready': [{'key': 'k6', 'line': '...'}],
                'confirmed': [],
            },
        ],
    )
    put_confirmations(voice, {'k5': 'n2'})

    reports = (await status(voice))['inputs']

    assert [r['confirm_again'] for r in reports] == [False, True]


async def test_a_partly_confirmed_update_is_not_to_confirm_again(voice):
    put_confirmations(voice, {'k1': 'n1'})

    (report,) = (await status(voice))['inputs']

    assert report['confirmed'] == ['k1']
    assert report['confirm_again'] is False


async def test_the_inputs_list_says_which_are_applied(voice):
    voice.inputs = [note('n1', 'time 1 is 1000'), note('n2', 'speed 4000')]
    put_confirmations(voice, {'k1': 'n1'})

    async with api(voice) as client:
        response = await client.get(
            f'/api/input-collections/{UPLOAD}/inputs',
            params={'collection_entry_id': COLLECTION},
        )

    assert response.status_code == HTTPStatus.OK
    assert [(i['entry_id'], i['applied']) for i in response.json()['inputs']] == [
        ('n1', True),
        ('n2', False),
    ]
