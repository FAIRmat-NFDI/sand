"""One by one, edits are decided in turn: an update's after the ones
above. The Confirm all review decides any update's, once all are read.
Undo last and Reset all take decisions back. Never an update a decision
above changed the sheet of, until the next run read it again."""

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


async def post(voice, action, keys, **body):
    async with api(voice) as client:
        return await client.post(
            f'/api/input-collections/{UPLOAD}/edits/{action}',
            params={'collection_entry_id': COLLECTION},
            json={'keys': keys, **body},
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
    """Update 2 is read on the sheet update 1 leaves: "from 10 s" only
    holds once update 1 is decided."""
    response = await post(voice, action, ['b1'])

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == (
        'decide the updates above first, or decide all at once with Confirm all'
    )
    # an update's own edits in any order
    assert (await post(voice, 'confirm', ['a2'])).status_code == HTTPStatus.OK
    assert (await post(voice, 'confirm', ['a1'])).status_code == HTTPStatus.OK
    assert (await post(voice, action, ['b1'])).status_code == HTTPStatus.OK


async def test_the_review_rejects_any_updates_edits(voice):
    response = await post(voice, 'reject', ['c1', 'a2'], review=True)

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json()['rejected'] == ['a2', 'c1']


async def test_the_review_confirms_every_undecided_edit_at_once(voice):
    response = await post(voice, 'confirm', ['a1', 'a2', 'b1', 'c1'], review=True)

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json()['confirmed'] == ['a1', 'a2', 'b1', 'c1']


async def test_the_review_confirms_all_or_nothing(voice):
    response = await post(voice, 'confirm', ['a1', 'b1'], review=True)

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == (
        'the edits changed since the review; look at them again'
    )
    assert CONFIRMATIONS_FILE not in voice.writer.files


async def test_the_review_waits_for_the_updates_below_a_rejection(voice):
    """Its rejections change what the updates below show: they are read
    again before the review confirms them."""
    rejected = await post(voice, 'reject', ['a2'], review=True)
    assert rejected.status_code == HTTPStatus.OK

    response = await post(voice, 'confirm', ['a1', 'b1', 'c1'], review=True)

    assert response.status_code == HTTPStatus.CONFLICT
    assert 'read again first' in response.json()['detail']


async def test_the_review_waits_for_an_update_not_read_yet(voice):
    voice.inputs = [*voice.inputs, note('n4', 'speed 2000')]

    response = await post(voice, 'reject', ['c1'], review=True)

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == 'an update is not read yet; wait for it'


async def test_the_review_waits_for_a_running_job(voice):
    put_status(voice, report(1, 'n1', 'a1'), phase='interpreting')

    response = await post(voice, 'reject', ['a1'], review=True)

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == (
        'the updates are being read; reject when it has finished'
    )


async def test_a_rejection_holds_back_the_updates_below_until_read_again(voice):
    put_status(voice, report(1, 'n1', 'a1', 'a2', rejected=[]), report(2, 'n2', 'b2'))
    assert (await post(voice, 'confirm', ['a1'])).status_code == HTTPStatus.OK
    assert (await post(voice, 'reject', ['a2'])).status_code == HTTPStatus.OK
    # update 2 was read with a2: its proposal may change with the next run
    response = await post(voice, 'confirm', ['b2'])
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == (
        'an update above changed: this one is read again first; '
        'confirm it when it is ready'
    )


async def test_a_rejection_in_a_run_from_before_reports_knew_them_waits(voice):
    """Such a report knew no rejections: a new one is since the run, and
    the updates below are decided after the run."""
    await post(voice, 'reject', ['a1', 'a2'])

    response = await post(voice, 'confirm', ['b1'])
    assert response.status_code == HTTPStatus.CONFLICT
    first = (await edits(voice))[0]
    assert sorted(first['rejected_since']) == ['a1', 'a2']


async def test_an_update_below_a_revised_one_waits_for_the_next_run(voice):
    voice.inputs = [note('n1', 'time 1 is 1200'), *voice.inputs[1:]]

    response = await post(voice, 'confirm', ['b1'])

    assert response.status_code == HTTPStatus.CONFLICT
    assert 'read again first' in response.json()['detail']


async def test_an_update_below_a_deleted_one_waits_for_the_next_run(voice):
    voice.inputs = voice.inputs[1:]

    response = await post(voice, 'reject', ['c1'])

    assert response.status_code == HTTPStatus.CONFLICT
    assert 'read again first' in response.json()['detail']


async def test_a_lost_decision_is_told_with_the_update(voice):
    """A decision above changed what update 2 shows: its rejection there
    matches none of its edits now."""
    put_status(voice, report(1, 'n1', 'a1', confirmed=['a1']), report(2, 'n2', 'b2'))
    put_confirmed(voice, a1='n1')
    voice.writer.files[REJECTIONS_FILE] = json.dumps(
        {
            'b1': {
                'by': 'user',
                'at': '2026-11-13T10:00:00+00:00',
                'input': 'n2',
                'reason': None,
                'meant': '',
                'proposed': None,
            }
        }
    ).encode()

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
    )
    put_confirmed(voice, b1='n2')

    first, second = await edits(voice)
    assert (second['confirmed'], second['held']) == (['b1'], ['b1'])
    assert second['confirmed_since'] == []  # no job is started for it
    assert (await post(voice, 'confirm', ['a1'])).status_code == HTTPStatus.OK
    assert (await edits(voice))[0]['confirmed_since'] == ['a1']


async def status(voice) -> dict:
    async with api(voice) as client:
        response = await client.get(
            f'/api/input-collections/{UPLOAD}/edit-status',
            params={'collection_entry_id': COLLECTION},
        )
    return response.json()


async def test_undo_last_takes_back_the_last_click(voice):
    await post(voice, 'confirm', ['a1'])
    await post(voice, 'reject', ['a2'])
    assert (await status(voice))['last_decision'] == [
        {'key': 'a2', 'decision': 'rejected'}
    ]

    response = await post(voice, 'undo-last', ['a2'])

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json() == {'undone': ['a2']}
    assert json.loads(voice.writer.files[REJECTIONS_FILE]) == {}
    assert list(json.loads(voice.writer.files[CONFIRMATIONS_FILE])) == ['a1']
    # the click before it is the last one now
    assert (await post(voice, 'undo-last', ['a2'])).status_code == HTTPStatus.CONFLICT
    assert (await post(voice, 'undo-last', ['a1'])).status_code == HTTPStatus.OK
    assert (await status(voice))['last_decision'] == []


async def test_undo_last_takes_back_a_whole_review(voice):
    await post(voice, 'confirm', ['a1', 'a2', 'b1', 'c1'], review=True)

    response = await post(voice, 'undo-last', ['a1', 'a2', 'b1', 'c1'])

    assert response.status_code == HTTPStatus.OK, response.text
    assert json.loads(voice.writer.files[CONFIRMATIONS_FILE]) == {}


async def test_a_rejection_taken_back_holds_back_the_updates_below(voice):
    put_status(voice, report(1, 'n1', 'a1', rejected=[]), report(2, 'n2', 'b1'))
    await post(voice, 'reject', ['a1'])
    # the next run knew it, and update 2 was confirmed and written
    put_status(
        voice,
        report(1, 'n1', 'a1', rejected=['a1']),
        report(2, 'n2', 'b1', confirmed=['b1'], rejected=[]),
    )

    assert (await post(voice, 'undo-last', ['a1'])).status_code == HTTPStatus.OK

    first, _ = await edits(voice)
    assert (first['rejected'], first['unrejected_since']) == ([], ['a1'])
    # update 2 was read without a1: read again before it is decided
    response = await post(voice, 'confirm', ['a1'])
    assert response.status_code == HTTPStatus.OK, response.text
    response = await post(voice, 'reject', ['b1'], review=True)
    assert response.status_code == HTTPStatus.CONFLICT


async def test_reset_takes_back_every_decision(voice):
    await post(voice, 'confirm', ['a1'])
    await post(voice, 'reject', ['a2'])

    async with api(voice) as client:
        response = await client.post(
            f'/api/input-collections/{UPLOAD}/edits/reset',
            params={'collection_entry_id': COLLECTION},
        )

    assert response.status_code == HTTPStatus.NO_CONTENT, response.text
    assert json.loads(voice.writer.files[CONFIRMATIONS_FILE]) == {}
    assert json.loads(voice.writer.files[REJECTIONS_FILE]) == {}


async def test_reset_with_a_damaged_file_changes_nothing(voice):
    await post(voice, 'confirm', ['a1'])
    confirmations = voice.writer.files[CONFIRMATIONS_FILE]
    voice.writer.files[REJECTIONS_FILE] = b'not json'

    async with api(voice) as client:
        response = await client.post(
            f'/api/input-collections/{UPLOAD}/edits/reset',
            params={'collection_entry_id': COLLECTION},
        )

    assert response.status_code == HTTPStatus.CONFLICT
    assert 'rejections file damaged' in response.json()['detail']
    assert voice.writer.files[CONFIRMATIONS_FILE] == confirmations


@pytest.mark.parametrize('action', ['undo-last', 'reset'])
async def test_decisions_are_taken_back_between_runs(voice, action):
    await post(voice, 'confirm', ['a1'])
    put_status(voice, report(1, 'n1', 'a1', 'a2'), phase='interpreting')

    response = await post(voice, action, ['a1'])

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
