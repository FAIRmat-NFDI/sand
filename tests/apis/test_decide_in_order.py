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
    put_status(voice, report(1, 'n1', 'a1', 'a2', rejected=[]), report(2, 'n2', 'b1'))
    assert (await post(voice, 'confirm', ['a1'])).status_code == HTTPStatus.OK
    # a2 is still to decide: update 1 stays open
    assert (await post(voice, 'confirm', ['b1'])).status_code == HTTPStatus.CONFLICT
    assert (await post(voice, 'reject', ['a2'])).status_code == HTTPStatus.OK
    # update 2 was read with a2: its proposal changes with the next run
    response = await post(voice, 'confirm', ['b1'])
    assert response.status_code == HTTPStatus.CONFLICT


async def test_a_confirmation_opens_the_next_update_at_once(voice):
    """Update 2 was read with update 1's edit: confirming it before the
    write keeps its key."""
    put_status(voice, report(1, 'n1', 'a1', rejected=[]), report(2, 'n2', 'b1'))
    assert (await post(voice, 'confirm', ['a1'])).status_code == HTTPStatus.OK
    assert (await post(voice, 'confirm', ['b1'])).status_code == HTTPStatus.OK


async def test_a_rejection_in_a_run_from_before_reports_knew_them_waits(voice):
    """Such a report knew no rejections: a new one is since the run, and
    the next update is decided after the run."""
    await post(voice, 'reject', ['a1', 'a2'])

    response = await post(voice, 'confirm', ['b1'])
    assert response.status_code == HTTPStatus.CONFLICT
    first = (await edits(voice))[0]
    assert sorted(first['rejected_since']) == ['a1', 'a2']


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


async def edits(voice) -> list[dict]:
    async with api(voice) as client:
        response = await client.get(
            f'/api/input-collections/{UPLOAD}/edits',
            params={'collection_entry_id': COLLECTION},
        )
    return response.json()['inputs']


async def test_a_held_edit_is_decided_and_waits_for_no_write(voice):
    # update 2 was confirmed, then update 1 reworded: read on a preview
    # with update 1's edit, it is held until update 1 is decided
    put_status(
        voice,
        report(1, 'n1', 'a1', rejected=[]),
        report(2, 'n2', 'b1', held=['b1'], rejected=[]),
    )
    put_confirmed(voice, b1='n2')

    first, second = await edits(voice)
    assert (second['confirmed'], second['held']) == (['b1'], ['b1'])
    assert second['confirmed_since'] == []  # no job is started for it
    assert (await post(voice, 'confirm', ['a1'])).status_code == HTTPStatus.OK
    assert (await edits(voice))[0]['confirmed_since'] == ['a1']


async def test_a_rejection_waits_for_the_run_before_the_next_update(voice):
    """The update after it was read with the edit: its proposal changes."""
    put_status(
        voice,
        report(1, 'n1', 'a1', rejected=[]),
        report(2, 'n2', 'b1', rejected=[]),
    )
    assert (await post(voice, 'reject', ['a1'])).status_code == HTTPStatus.OK

    first, _ = await edits(voice)
    assert first['rejected_since'] == ['a1']
    response = await post(voice, 'confirm', ['b1'])
    assert response.status_code == HTTPStatus.CONFLICT
    # the run knows it: update 2 is open
    put_status(
        voice,
        report(1, 'n1', 'a1', rejected=['a1']),
        report(2, 'n2', 'b2', rejected=[]),
    )
    assert (await post(voice, 'confirm', ['b2'])).status_code == HTTPStatus.OK


async def test_a_rejection_taken_back_reopens_its_update(voice):
    put_status(voice, report(1, 'n1', 'a1', rejected=[]), report(2, 'n2', 'b1'))
    await post(voice, 'reject', ['a1'])
    # the next run knew it, and update 2 was confirmed and written
    put_status(
        voice,
        report(1, 'n1', 'a1', rejected=['a1']),
        report(2, 'n2', 'b1', confirmed=['b1'], rejected=[]),
    )
    put_confirmed(voice, b1='n2')

    assert (await post(voice, 'undo-reject', ['a1'])).status_code == HTTPStatus.OK

    first, second = await edits(voice)
    assert (first['rejected'], first['unrejected_since']) == ([], ['a1'])
    # update 2 was read without a1: it leaves the sheet with the next run
    assert second['confirmed'] == ['b1']
    response = await post(voice, 'confirm', ['a1'])
    assert response.status_code == HTTPStatus.OK, response.text


async def test_a_shared_key_confirmed_by_one_input_is_not_its_rejection(voice):
    """Two updates of the same words share a key: one confirmed it, the
    other rejected it. The run lists the key as rejected for both."""
    put_status(
        voice,
        report(1, 'n1', 'k1', confirmed=['k1'], rejected=['k1']),
        report(2, 'n2', 'k1', rejected=['k1']),
    )
    put_confirmed(voice, k1='n1')
    voice.writer.files[REJECTIONS_FILE] = json.dumps(
        {
            'k1': {
                'by': 'user',
                'at': '2026-11-13T10:00:00+00:00',
                'input': 'n2',
                'reason': None,
                'meant': '',
                'proposed': None,
            }
        }
    ).encode()

    first, second = await edits(voice)
    assert first['unrejected_since'] == []
    assert (second['rejected'], second['rejected_since']) == (['k1'], [])


async def test_a_damaged_rejections_file_keeps_the_runs_rejections(voice):
    put_status(
        voice,
        report(1, 'n1', 'a1', rejected=['a1']),
        report(2, 'n2', 'b1', rejected=[]),
    )
    voice.writer.files[REJECTIONS_FILE] = b'not json'

    first, second = await edits(voice)
    assert first['rejected'] == ['a1']
    assert (first['rejected_since'], first['unrejected_since']) == ([], [])
    assert second['rejected'] == []
