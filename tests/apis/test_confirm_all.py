"""Confirm all (#208): every undecided edit at once, once every update is
read and nothing changed since; Reset all takes every decision back.
Never an update a decision above changed the sheet of, until the next
run read it again."""

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


def put_status(voice, *reports, phase='completed'):
    voice.writer.files[STATUS_FILE] = json.dumps(
        {
            'job_id': 'job-0',
            'phase': phase,
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


async def post(voice, action, body):
    async with api(voice) as client:
        return await client.post(
            f'/api/input-collections/{UPLOAD}/edits/{action}',
            params={'collection_entry_id': COLLECTION},
            json=body,
        )


async def confirm_all(voice, keys):
    return await post(voice, 'confirm', {'keys': keys})


async def untick(voice, *keys):
    return await post(voice, 'round', {'reject': [{'key': k} for k in keys]})


def put_rejected(voice, **keys):
    voice.writer.files[REJECTIONS_FILE] = json.dumps(
        {
            key: {
                'by': 'user',
                'at': '2026-11-13T10:00:00+00:00',
                'input': entry_id,
                'reason': None,
                'meant': '',
                'proposed': None,
            }
            for key, entry_id in keys.items()
        }
    ).encode()


@pytest.fixture
def voice() -> FakeVoice:
    voice = FakeVoice()
    voice.inputs = [note(e, t) for e, t in TEXTS.items()]
    put_status(
        voice, report(1, 'n1', 'a1', 'a2'), report(2, 'n2', 'b1'), report(3, 'n3', 'c1')
    )
    return voice


ALL = ['a1', 'a2', 'b1', 'c1']


async def test_confirm_all_confirms_every_undecided_edit_at_once(voice):
    response = await confirm_all(voice, ALL)

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json()['confirmed'] == ALL


async def test_confirm_all_is_all_or_nothing(voice):
    response = await confirm_all(voice, ['a1', 'b1'])

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == (
        'the edits changed since they were shown; look at them again'
    )
    assert CONFIRMATIONS_FILE not in voice.writer.files


async def test_confirm_all_waits_for_the_updates_below_an_untick(voice):
    """An untick changes what the updates below show: they are read
    again before anything is confirmed."""
    assert (await untick(voice, 'a2')).status_code == HTTPStatus.OK

    response = await confirm_all(voice, ['a1', 'b1', 'c1'])

    assert response.status_code == HTTPStatus.CONFLICT
    assert 'read again first' in response.json()['detail']


async def test_confirm_all_waits_for_an_update_not_read_yet(voice):
    voice.inputs = [*voice.inputs, note('n4', 'speed 2000')]

    response = await confirm_all(voice, ALL)

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == 'an update is not read yet; wait for it'


async def test_confirm_all_waits_for_a_running_job(voice):
    put_status(voice, report(1, 'n1', 'a1'), phase='interpreting')

    response = await confirm_all(voice, ['a1'])

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == (
        'the updates are being read; confirm when it has finished'
    )


async def test_an_untick_in_a_run_from_before_reports_knew_them_waits(voice):
    """Such a report knew no rejections: a new one is since the run, and
    the updates below are confirmed after the run."""
    await untick(voice, 'a1', 'a2')

    assert (await confirm_all(voice, ['b1', 'c1'])).status_code == HTTPStatus.CONFLICT
    first = (await edits(voice))[0]
    assert sorted(first['rejected_since']) == ['a1', 'a2']


@pytest.mark.parametrize('change', ['revise', 'delete'])
async def test_an_update_below_a_changed_one_waits_for_the_next_run(voice, change):
    if change == 'revise':
        voice.inputs = [note('n1', 'time 1 is 1200'), *voice.inputs[1:]]
    else:
        voice.inputs = voice.inputs[1:]

    response = await confirm_all(voice, ALL if change == 'revise' else ['b1', 'c1'])

    assert response.status_code == HTTPStatus.CONFLICT


async def test_a_lost_decision_is_told_with_the_update(voice):
    """A decision above changed what update 2 shows: its rejection there
    matches none of its edits now."""
    put_status(voice, report(1, 'n1', 'a1', confirmed=['a1']), report(2, 'n2', 'b2'))
    put_confirmed(voice, a1='n1')
    put_rejected(voice, b1='n2')

    _, second = await edits(voice)
    assert second['decide_again'] == 1


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
        report(3, 'n3', rejected=[]),
    )
    put_confirmed(voice, b1='n2')

    _, second, _ = await edits(voice)
    assert (second['confirmed'], second['held']) == (['b1'], ['b1'])
    assert second['confirmed_since'] == []  # no job is started for it
    assert (await confirm_all(voice, ['a1'])).status_code == HTTPStatus.OK
    assert (await edits(voice))[0]['confirmed_since'] == ['a1']


async def test_a_rejection_ticked_again_holds_back_the_updates_below(voice):
    put_status(voice, report(1, 'n1', 'a1', rejected=['a1']), report(2, 'n2', 'b1'))
    put_rejected(voice, a1='n1')

    restored = await post(voice, 'round', {'restore': ['a1']})

    assert restored.status_code == HTTPStatus.OK, restored.text
    first, _ = await edits(voice)
    assert (first['rejected'], first['unrejected_since']) == ([], ['a1'])
    # update 2 was read without a1: read again before it is confirmed
    assert (await confirm_all(voice, ['a1', 'b1'])).status_code == HTTPStatus.CONFLICT


async def reset(voice):
    async with api(voice) as client:
        return await client.post(
            f'/api/input-collections/{UPLOAD}/edits/reset',
            params={'collection_entry_id': COLLECTION},
        )


async def test_reset_takes_back_every_decision(voice):
    put_confirmed(voice, a1='n1')
    put_rejected(voice, a2='n1')

    response = await reset(voice)

    assert response.status_code == HTTPStatus.NO_CONTENT, response.text
    assert json.loads(voice.writer.files[CONFIRMATIONS_FILE]) == {}
    assert json.loads(voice.writer.files[REJECTIONS_FILE]) == {}


async def test_reset_with_a_damaged_file_changes_nothing(voice):
    put_confirmed(voice, a1='n1')
    confirmations = voice.writer.files[CONFIRMATIONS_FILE]
    voice.writer.files[REJECTIONS_FILE] = b'not json'

    response = await reset(voice)

    assert response.status_code == HTTPStatus.CONFLICT
    assert 'rejections file damaged' in response.json()['detail']
    assert voice.writer.files[CONFIRMATIONS_FILE] == confirmations


async def test_decisions_are_taken_back_between_runs(voice):
    put_confirmed(voice, a1='n1')
    put_status(voice, report(1, 'n1', 'a1', 'a2'), phase='interpreting')

    response = await reset(voice)

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == 'the updates are being read; wait for it'


async def test_a_shared_key_confirmed_by_one_input_is_not_its_rejection(voice):
    """Two updates of the same words share a key: one confirmed it, the
    other rejected it. The run lists the key as rejected for both."""
    put_status(
        voice,
        report(1, 'n1', 'k1', confirmed=['k1'], rejected=['k1']),
        report(2, 'n2', 'k1', rejected=['k1']),
    )
    put_confirmed(voice, k1='n1')
    put_rejected(voice, k1='n2')

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
