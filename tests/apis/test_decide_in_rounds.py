"""Deciding in rounds (#208): a round unticks edits of any input and ticks
rejected ones again, in one write; Confirm all locks the inputs it
covered until Reset all."""

import json
from http import HTTPStatus

import pytest
from nomad_fakes import COLLECTION, UPLOAD, FakeVoice, api, note
from test_decide_in_turn import TEXTS, put_confirmed, put_status, report

from sand.sheet_edit.session import (
    CHECKPOINT_FILE,
    CONFIRMATIONS_FILE,
    REJECTIONS_FILE,
)

pytestmark = pytest.mark.asyncio

PARAMS = {'collection_entry_id': COLLECTION}


@pytest.fixture
def voice() -> FakeVoice:
    voice = FakeVoice()
    voice.inputs = [note(e, t) for e, t in TEXTS.items()]
    # input 3 is skipped: nothing ready to confirm
    put_status(
        voice, report(1, 'n1', 'a1', 'a2'), report(2, 'n2', 'b1'), report(3, 'n3')
    )
    return voice


async def round_(voice, reject=(), restore=()):
    async with api(voice) as client:
        return await client.post(
            f'/api/input-collections/{UPLOAD}/edits/round',
            params=PARAMS,
            json={'reject': list(reject), 'restore': list(restore)},
        )


async def confirm_all(voice, keys):
    async with api(voice) as client:
        return await client.post(
            f'/api/input-collections/{UPLOAD}/edits/confirm',
            params=PARAMS,
            json={'keys': keys, 'review': True},
        )


def rejections(voice) -> dict:
    return json.loads(voice.writer.files.get(REJECTIONS_FILE, b'{}'))


def put_rejected(voice, **keys):
    voice.writer.files[REJECTIONS_FILE] = json.dumps(
        {
            key: {
                'by': 'user',
                'at': '2026-11-18T10:00:00+00:00',
                'input': entry_id,
                'reason': None,
                'meant': '',
                'proposed': None,
            }
            for key, entry_id in keys.items()
        }
    ).encode()


async def test_a_round_unticks_edits_of_any_input_in_one_write(voice):
    response = await round_(
        voice,
        reject=[{'key': 'b1', 'reason': 'value', 'meant': '3000'}, {'key': 'a2'}],
    )

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json()['rejected'] == ['a2', 'b1']
    stored = rejections(voice)
    assert (stored['b1']['input'], stored['b1']['meant']) == ('n2', '3000')
    assert stored['a2']['reason'] is None


async def test_a_round_ticks_a_rejected_edit_again(voice):
    put_rejected(voice, a2='n1')

    response = await round_(voice, restore=['a2'])

    assert response.status_code == HTTPStatus.OK, response.text
    assert rejections(voice) == {}


async def test_a_rejection_matching_no_edit_can_be_ticked_again(voice):
    # its edit changed with the sheet: "untick no longer applies"
    put_rejected(voice, gone='n1', a2='n1')

    response = await round_(voice, restore=['gone'])

    assert response.status_code == HTTPStatus.OK, response.text
    assert list(rejections(voice)) == ['a2']


async def test_a_round_drops_rejections_that_match_no_edit(voice):
    put_rejected(voice, gone='n1')

    response = await round_(voice, reject=[{'key': 'a1'}])

    assert response.json()['rejected'] == ['a1']


@pytest.mark.parametrize(
    ('reject', 'restore', 'code'),
    [
        ([], [], HTTPStatus.BAD_REQUEST),
        ([{'key': 'a1'}], ['a1'], HTTPStatus.BAD_REQUEST),
        ([], ['a1'], HTTPStatus.UNPROCESSABLE_ENTITY),  # not rejected
        ([{'key': 'zz'}], [], HTTPStatus.UNPROCESSABLE_ENTITY),  # not an edit
    ],
)
async def test_a_round_that_makes_no_sense_is_refused(voice, reject, restore, code):
    response = await round_(voice, reject=reject, restore=restore)

    assert response.status_code == code
    assert REJECTIONS_FILE not in voice.writer.files


async def test_a_round_waits_for_a_running_job(voice):
    put_status(voice, report(1, 'n1', 'a1'), phase='interpreting')

    response = await round_(voice, restore=['a1'])

    assert response.status_code == HTTPStatus.CONFLICT


async def test_a_round_cannot_untick_a_confirmed_edit(voice):
    put_confirmed(voice, a1='n1')

    response = await round_(voice, reject=[{'key': 'a1'}])

    assert response.status_code == HTTPStatus.CONFLICT


async def test_a_round_goes_with_the_notes_changed_in_it(voice):
    """Edit note 1 and untick note 2's edit, one round: the untick is on
    the last run's edit; read again, it may no longer apply."""
    voice.inputs[0] = note('n1', 'time 1 is 1200')

    response = await round_(voice, reject=[{'key': 'b1'}])

    assert response.status_code == HTTPStatus.OK, response.text
    assert list(rejections(voice)) == ['b1']


async def test_rounds_follow_each_other_before_a_reread(voice):
    assert (await round_(voice, reject=[{'key': 'a2'}])).status_code == HTTPStatus.OK

    response = await round_(voice, reject=[{'key': 'b1'}])

    assert response.status_code == HTTPStatus.OK
    assert response.json()['rejected'] == ['a2', 'b1']


async def test_confirm_all_locks_every_input_it_covered(voice):
    response = await confirm_all(voice, ['a1', 'a2', 'b1'])

    assert response.status_code == HTTPStatus.OK, response.text
    # the skipped input 3 too, and with the confirmations in one request
    checkpoint = json.loads(voice.writer.files[CHECKPOINT_FILE])
    assert checkpoint['inputs'] == ['n1', 'n2', 'n3']
    assert CONFIRMATIONS_FILE in voice.writer.files


async def locked_voice(voice) -> FakeVoice:
    voice.writer.files[CHECKPOINT_FILE] = json.dumps({'inputs': ['n1']}).encode()
    return voice


async def test_a_locked_input_cannot_be_revised_or_deleted(voice):
    await locked_voice(voice)
    async with api(voice) as client:
        revised = await client.post(
            f'/api/input-collections/{UPLOAD}/inputs/n1/text',
            params=PARAMS,
            json={'text': 'time 1 is 1200'},
        )
        deleted = await client.delete(
            f'/api/input-collections/{UPLOAD}/inputs/n1', params=PARAMS
        )
        other = await client.post(
            f'/api/input-collections/{UPLOAD}/inputs/n2/text',
            params=PARAMS,
            json={'text': 'speed 3000'},
        )

    assert revised.status_code == deleted.status_code == HTTPStatus.CONFLICT
    assert revised.json()['detail'] == (
        'confirmed with Confirm all; Reset all to change it'
    )
    assert voice.removed == []
    assert other.status_code == HTTPStatus.OK


async def test_the_inputs_list_says_which_are_locked(voice):
    await locked_voice(voice)
    async with api(voice) as client:
        response = await client.get(
            f'/api/input-collections/{UPLOAD}/inputs', params=PARAMS
        )

    assert [i['locked'] for i in response.json()['inputs']] == [True, False, False]


async def test_a_round_cannot_decide_a_locked_input(voice):
    await locked_voice(voice)

    response = await round_(voice, reject=[{'key': 'a2'}])

    assert response.status_code == HTTPStatus.CONFLICT


async def test_reset_all_unlocks(voice):
    await locked_voice(voice)
    async with api(voice) as client:
        response = await client.post(
            f'/api/input-collections/{UPLOAD}/edits/reset', params=PARAMS
        )

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert json.loads(voice.writer.files[CHECKPOINT_FILE]) == {}


async def undo_last(voice, keys):
    async with api(voice) as client:
        return await client.post(
            f'/api/input-collections/{UPLOAD}/edits/undo-last',
            params=PARAMS,
            json={'keys': keys},
        )


async def test_undo_last_of_a_confirm_all_unlocks_its_inputs(voice):
    await confirm_all(voice, ['a1', 'a2', 'b1'])

    response = await undo_last(voice, ['a1', 'a2', 'b1'])

    assert response.status_code == HTTPStatus.OK, response.text
    assert json.loads(voice.writer.files[CHECKPOINT_FILE]) == {}
    assert (await round_(voice, reject=[{'key': 'a2'}])).status_code == HTTPStatus.OK


async def test_undo_last_of_a_confirm_all_keeps_the_locks_before_it(voice):
    await locked_voice(voice)  # an earlier Confirm all locked n1
    await confirm_all(voice, ['b1'])

    await undo_last(voice, ['b1'])

    assert json.loads(voice.writer.files[CHECKPOINT_FILE]) == {'inputs': ['n1']}


async def test_confirm_all_fails_when_nomad_stored_nothing(voice, monkeypatch):
    async def lost(client, upload_id, files, directory=''):
        pass  # acknowledged, not stored

    monkeypatch.setattr(voice.writer, 'upload_raw_files', lost)

    response = await confirm_all(voice, ['a1', 'a2', 'b1'])

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert 'did not store' in response.json()['detail']


async def test_a_key_ticked_again_twice_is_one_restore(voice):
    put_rejected(voice, a2='n1')

    response = await round_(voice, restore=['a2', 'a2'])

    assert response.status_code == HTTPStatus.OK, response.text
    assert rejections(voice) == {}
