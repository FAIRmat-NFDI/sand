"""Edits are decided in order: only the first update with an undecided
edit takes a confirmation or a rejection."""

import json
from datetime import datetime, timezone
from http import HTTPStatus

import pytest
from nomad_fakes import COLLECTION, UPLOAD, FakeVoice, api, note

from sand.sheet_edit.session import CONFIRMATIONS_FILE, REJECTIONS_FILE, STATUS_FILE

pytestmark = pytest.mark.asyncio

TEXTS = {'n1': 'time 1 is 1100', 'n2': 'speed 4000', 'n3': 'anneal 120'}


def report(number, entry_id, *keys, **fields):
    return {
        'input': number,
        'entry_id': entry_id,
        'text': TEXTS[entry_id],
        'ready': [{'key': key, 'line': '...'} for key in keys],
        'confirmed': [],
        **fields,
    }


def put_status(voice, *reports):
    voice.writer.files[STATUS_FILE] = json.dumps(
        {
            'job_id': 'job-0',
            'phase': 'completed',
            'collection_entry_id': COLLECTION,
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'inputs': list(reports),
        }
    ).encode()


def put_confirmed(voice, **keys):
    voice.writer.files[CONFIRMATIONS_FILE] = json.dumps(
        {
            key: {'by': 'user', 'at': '2026-11-13T10:00:00+00:00', 'input': entry_id}
            for key, entry_id in keys.items()
        }
    ).encode()


async def post(voice, action, keys):
    async with api(voice) as client:
        return await client.post(
            f'/api/input-collections/{UPLOAD}/edits/{action}',
            params={'collection_entry_id': COLLECTION},
            json={'keys': keys},
        )


@pytest.fixture
def voice() -> FakeVoice:
    voice = FakeVoice()
    voice.inputs = [note(e, t) for e, t in TEXTS.items()]
    put_status(
        voice, report(1, 'n1', 'a1', 'a2'), report(2, 'n2', 'b1'), report(3, 'n3', 'c1')
    )
    return voice


@pytest.mark.parametrize('action', ['confirm', 'reject'])
async def test_a_later_updates_edit_waits_for_the_ones_above(voice, action):
    response = await post(voice, action, ['b1'])

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == 'decide the updates above first'
    # one of the open update's and one of a later one's: neither
    assert (await post(voice, action, ['a1', 'b1'])).status_code == HTTPStatus.CONFLICT
    assert CONFIRMATIONS_FILE not in voice.writer.files
    assert REJECTIONS_FILE not in voice.writer.files


async def test_the_open_update_is_decided_edit_by_edit(voice):
    assert (await post(voice, 'confirm', ['a1'])).status_code == HTTPStatus.OK
    # a1 is waiting to be written: update 1 stays open, a2 is its
    assert (await post(voice, 'reject', ['a2'])).status_code == HTTPStatus.OK
    # update 1's confirmation changes the sheet update 2 is read on
    response = await post(voice, 'confirm', ['b1'])
    assert response.status_code == HTTPStatus.CONFLICT


async def test_a_rejection_opens_the_next_update_at_once(voice):
    await post(voice, 'reject', ['a1', 'a2'])

    assert (await post(voice, 'confirm', ['b1'])).status_code == HTTPStatus.OK


async def test_the_next_update_opens_once_the_sheet_is_written(voice):
    put_status(
        voice,
        report(1, 'n1', 'a1', 'a2', confirmed=['a1', 'a2']),
        report(2, 'n2', 'b1'),
        report(3, 'n3', 'c1', waiting=True),
    )
    put_confirmed(voice, a1='n1', a2='n1')

    assert (await post(voice, 'confirm', ['b1'])).status_code == HTTPStatus.OK
    # sending a decided key again is fine
    assert (await post(voice, 'confirm', ['a1', 'b1'])).status_code == HTTPStatus.OK


async def test_a_waiting_update_is_read_again_before_it_is_decided(voice):
    put_status(
        voice,
        report(1, 'n1', 'a1', confirmed=['a1']),
        report(2, 'n2', 'b1', waiting=True),
        report(3, 'n3', 'c1', waiting=True),
    )
    put_confirmed(voice, a1='n1')

    response = await post(voice, 'reject', ['b1'])

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == (
        'this update is read again first; decide it when it is ready'
    )


async def test_a_waiting_report_shows_nothing_waiting_to_be_written(voice):
    # its keys are of a sheet it is not read on yet; a confirmation of
    # it from before is not counted as written or lost
    put_status(
        voice,
        report(1, 'n1', 'a1'),
        report(2, 'n2', 'b9', waiting=True),
        report(3, 'n3', 'c1'),
    )
    put_confirmed(voice, b1='n2')

    async with api(voice) as client:
        response = await client.get(
            f'/api/input-collections/{UPLOAD}/edits',
            params={'collection_entry_id': COLLECTION},
        )
    two = response.json()['inputs'][1]
    assert two['confirmed_since'] == []
    assert two['unconfirmed_since'] == []
    assert two['confirm_again'] == 0


async def test_a_waiting_update_without_edits_still_holds_the_ones_after(voice):
    # its earlier answer gives nothing on today's sheet: it is read
    # again before anything after it is decided
    put_status(
        voice,
        report(1, 'n1', 'a1', confirmed=['a1']),
        report(2, 'n2', waiting=True),
        report(3, 'n3', 'c1'),
    )
    put_confirmed(voice, a1='n1')

    response = await post(voice, 'confirm', ['c1'])

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == 'decide the updates above first'
