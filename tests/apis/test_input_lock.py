"""A confirmed update is a record: its words cannot be changed and it
cannot be deleted; a correction is a new update."""

import json
from http import HTTPStatus

import pytest
from nomad_fakes import COLLECTION, UPLOAD, FakeVoice, api

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


def put_confirmations(voice, *keys):
    voice.writer.files[CONFIRMATIONS_FILE] = json.dumps(
        {key: {'by': 'user', 'at': '2026-10-28T12:16:05+00:00'} for key in keys}
    ).encode()


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


@pytest.fixture
def voice() -> FakeVoice:
    voice = FakeVoice()
    put_status(voice, [{'input': 1, 'entry_id': 'n1', 'ready': READY, 'confirmed': []}])
    return voice


async def test_a_confirmed_update_keeps_its_words(voice):
    put_confirmations(voice, 'k2')

    response = await revise(voice)

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == (
        'a confirmed update is a record and cannot be changed; add a new update instead'
    )
    assert voice.revised == []


async def test_a_confirmed_update_stays(voice):
    put_confirmations(voice, 'k1')

    response = await delete(voice)

    assert response.status_code == HTTPStatus.CONFLICT
    assert 'cannot be deleted' in response.json()['detail']
    assert voice.removed == []


async def test_the_jobs_own_confirmed_list_counts_too(voice):
    put_status(
        voice, [{'input': 1, 'entry_id': 'n1', 'ready': READY, 'confirmed': ['k1']}]
    )

    assert (await revise(voice)).status_code == HTTPStatus.CONFLICT


async def test_an_unconfirmed_update_can_change(voice):
    put_confirmations(voice, 'k9')  # somebody else's key

    assert (await revise(voice)).status_code == HTTPStatus.OK
    assert voice.revised == [('n1', 'time 1 is 1100')]
    assert (await delete(voice)).status_code == HTTPStatus.NO_CONTENT
    assert voice.removed == ['n1']


async def test_another_input_is_not_locked_by_this_one(voice):
    put_confirmations(voice, 'k1')

    assert (await revise(voice, 'n2')).status_code == HTTPStatus.OK


async def test_no_job_yet_or_another_collections_job_locks_nothing():
    voice = FakeVoice()
    assert (await revise(voice)).status_code == HTTPStatus.OK
    put_status(
        voice,
        [{'input': 1, 'entry_id': 'n1', 'ready': READY, 'confirmed': ['k1']}],
        collection='collection-2',
    )
    assert (await revise(voice)).status_code == HTTPStatus.OK
