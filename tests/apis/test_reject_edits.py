"""Rejecting a proposed edit: recorded with what was proposed, never
written, decided; undone on request; gone when its update changes."""

import json
from datetime import datetime, timezone
from http import HTTPStatus

import pytest
from nomad_fakes import COLLECTION, UPLOAD, USERNAME, FakeVoice, api, note

from sand.sheet_edit.session import CONFIRMATIONS_FILE, REJECTIONS_FILE, STATUS_FILE

pytestmark = pytest.mark.asyncio

READY = [
    {'key': 'k1', 'line': 'time 1000 → 1100', 'column': 'Time [s]'},
    {'key': 'k2', 'line': 'speed 3000 → 4000'},
]
TEXT = 'time 1 is 1100, speed 4000'


def put_status(voice, phase='completed', reports=None):
    voice.writer.files[STATUS_FILE] = json.dumps(
        {
            'job_id': 'job-0',
            'phase': phase,
            'collection_entry_id': COLLECTION,
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'inputs': reports
            or [
                {
                    'input': 1,
                    'entry_id': 'n1',
                    'text': TEXT,
                    'ready': READY,
                    'confirmed': [],
                }
            ],
        }
    ).encode()


def record(entry_id) -> dict:
    """A stored rejection of this input."""
    return {
        'by': 'user',
        'at': '2026-11-12T10:00:00+00:00',
        'input': entry_id,
        'reason': None,
        'meant': '',
        'proposed': None,
    }


def rejections(voice) -> dict:
    return json.loads(voice.writer.files[REJECTIONS_FILE])


async def post(voice, action, body):
    async with api(voice) as client:
        return await client.post(
            f'/api/input-collections/{UPLOAD}/edits/{action}',
            params={'collection_entry_id': COLLECTION},
            json=body,
        )


async def report(voice):
    async with api(voice) as client:
        response = await client.get(
            f'/api/input-collections/{UPLOAD}/edits',
            params={'collection_entry_id': COLLECTION},
        )
    [only] = response.json()['inputs']
    return only


@pytest.fixture
def voice() -> FakeVoice:
    voice = FakeVoice()
    voice.inputs = [note('n1', TEXT)]
    put_status(voice)
    return voice


async def test_a_rejection_keeps_the_edit_as_proposed_and_the_reason(voice):
    response = await post(
        voice, 'reject', {'keys': ['k1'], 'reason': 'value', 'meant': ' 1200 s '}
    )

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json() == {'rejected': ['k1']}
    record = rejections(voice)['k1']
    assert record['by'] == USERNAME
    assert record['input'] == 'n1'
    assert record['reason'] == 'value'
    assert record['meant'] == '1200 s'
    assert record['proposed']['line'] == 'time 1000 → 1100'
    assert record['proposed']['text'] == TEXT
    # nothing the job reads
    assert CONFIRMATIONS_FILE not in voice.writer.files


async def test_the_reason_is_optional(voice):
    response = await post(voice, 'reject', {'keys': ['k1']})

    assert response.status_code == HTTPStatus.OK, response.text
    assert rejections(voice)['k1']['reason'] is None


async def test_a_rejected_edit_is_decided_and_shown(voice):
    await post(voice, 'reject', {'keys': ['k1']})

    shown = await report(voice)

    assert shown['rejected'] == ['k1']
    assert shown['confirmed'] == []


async def test_a_rejected_key_keeps_its_first_record(voice):
    await post(voice, 'reject', {'keys': ['k1'], 'reason': 'value'})
    await post(voice, 'reject', {'keys': ['k1', 'k2'], 'reason': 'column'})

    records = rejections(voice)
    assert records['k1']['reason'] == 'value'
    assert records['k2']['reason'] == 'column'


async def test_a_confirmed_edit_cannot_be_rejected(voice):
    assert (await post(voice, 'confirm', {'keys': ['k1']})).status_code == HTTPStatus.OK

    response = await post(voice, 'reject', {'keys': ['k1', 'k2']})

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == 'already confirmed: k1'
    assert REJECTIONS_FILE not in voice.writer.files


async def test_a_rejected_edit_is_not_confirmed_until_undone(voice):
    await post(voice, 'reject', {'keys': ['k1']})

    response = await post(voice, 'confirm', {'keys': ['k1']})

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == 'rejected, undo the rejection first: k1'
    assert CONFIRMATIONS_FILE not in voice.writer.files

    undone = await post(voice, 'undo-reject', {'keys': ['k1']})
    assert undone.status_code == HTTPStatus.OK
    assert undone.json() == {'rejected': []}
    assert (await report(voice))['rejected'] == []
    assert (await post(voice, 'confirm', {'keys': ['k1']})).status_code == HTTPStatus.OK


async def test_undoing_a_key_not_rejected_is_fine(voice):
    response = await post(voice, 'undo-reject', {'keys': ['k9']})

    assert response.status_code == HTTPStatus.OK
    assert REJECTIONS_FILE not in voice.writer.files


async def test_only_a_ready_edit_of_the_last_job_can_be_rejected(voice):
    response = await post(voice, 'reject', {'keys': ['k9']})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()['detail'] == 'not a ready edit of the last job: k9'


async def test_no_keys(voice):
    response = await post(voice, 'reject', {'keys': []})

    assert response.status_code == HTTPStatus.BAD_REQUEST


async def test_a_key_the_running_job_has_not_reported_yet_waits(voice):
    put_status(voice, 'interpreting')

    response = await post(voice, 'reject', {'keys': ['k9']})

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == (
        'an edit job is running; reject when it has finished'
    )


async def test_words_revised_since_the_run_are_not_rejected(voice):
    voice.inputs = [note('n1', 'time 1 is 1200')]

    response = await post(voice, 'reject', {'keys': ['k1']})

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == (
        'the words of an input changed since the last run; apply the updates again, then reject'
    )


async def test_a_damaged_rejections_file_is_never_overwritten(voice):
    voice.writer.files[REJECTIONS_FILE] = b'{not json'

    response = await post(voice, 'reject', {'keys': ['k1']})

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'].startswith('rejections file damaged')
    assert voice.writer.files[REJECTIONS_FILE] == b'{not json'
    # the page still shows the reports, with nothing rejected
    assert (await report(voice))['rejected'] == []


async def test_a_rejection_holds_for_every_input_with_its_key(voice):
    # the same words on the same sheet: one key, and nothing written
    # that would make the second input's key differ
    voice.inputs = [note('n1', TEXT), note('n2', TEXT)]
    put_status(
        voice,
        reports=[
            {'input': i, 'entry_id': e, 'text': TEXT, 'ready': READY, 'confirmed': []}
            for i, e in ((1, 'n1'), (2, 'n2'))
        ],
    )
    await post(voice, 'reject', {'keys': ['k1']})

    async with api(voice) as client:
        response = await client.get(
            f'/api/input-collections/{UPLOAD}/edits',
            params={'collection_entry_id': COLLECTION},
        )
    assert [r['rejected'] for r in response.json()['inputs']] == [['k1'], ['k1']]
    # confirming the other edit of both is fine
    assert (await post(voice, 'confirm', {'keys': ['k2']})).status_code == HTTPStatus.OK


async def test_rejecting_an_input_again_drops_its_lost_rejections(voice):
    voice.writer.files[REJECTIONS_FILE] = json.dumps(
        {
            'old': record('n1'),
            'other': record('n2'),
        }
    ).encode()

    await post(voice, 'reject', {'keys': ['k2']})

    assert sorted(rejections(voice)) == ['k2', 'other']


async def test_an_input_with_every_edit_decided_has_nothing_to_confirm_again(voice):
    # one confirmation lost to an earlier update's change, the edits now
    # all decided: nothing is waiting
    voice.writer.files[CONFIRMATIONS_FILE] = json.dumps(
        {'gone': {'by': 'user', 'at': '2026-11-12T10:00:00+00:00', 'input': 'n1'}}
    ).encode()
    await post(voice, 'reject', {'keys': ['k1', 'k2']})

    assert (await report(voice))['confirm_again'] == 0


async def test_deciding_an_input_again_drops_its_lost_decisions_of_both_kinds(voice):
    lost = {'by': 'user', 'at': '2026-11-12T10:00:00+00:00', 'input': 'n1'}
    voice.writer.files[CONFIRMATIONS_FILE] = json.dumps({'old-c': lost}).encode()
    voice.writer.files[REJECTIONS_FILE] = json.dumps({'old-r': record('n1')}).encode()

    await post(voice, 'reject', {'keys': ['k1']})
    assert json.loads(voice.writer.files[CONFIRMATIONS_FILE]) == {}
    assert sorted(rejections(voice)) == ['k1']

    voice.writer.files[REJECTIONS_FILE] = json.dumps({'old-r': record('n1')}).encode()
    await post(voice, 'confirm', {'keys': ['k2']})
    assert rejections(voice) == {}


@pytest.mark.parametrize('change', ['revise', 'delete'])
async def test_revising_or_deleting_an_update_drops_its_rejections(voice, change):
    voice.inputs = [note('n1', TEXT), note('n2', 'speed 5000')]
    voice.writer.files[REJECTIONS_FILE] = json.dumps(
        {
            'k1': record('n1'),
            'k7': record('n2'),
        }
    ).encode()

    async with api(voice) as client:
        url = f'/api/input-collections/{UPLOAD}/inputs/n1'
        params = {'collection_entry_id': COLLECTION}
        if change == 'revise':
            response = await client.post(
                url + '/text', params=params, json={'text': 'time 1 is 1300'}
            )
        else:
            response = await client.delete(url, params=params)

    assert response.status_code in (HTTPStatus.OK, HTTPStatus.NO_CONTENT), response.text
    assert sorted(rejections(voice)) == ['k7']


@pytest.mark.parametrize(
    'damage',
    [
        {'input': 'n1'},  # fields missing
        {**record('n1'), 'by': ''},
        {**record('n1'), 'at': '2026-11-12T10:00:00'},  # no timezone
        {**record('n1'), 'reason': 'typo'},
        {**record('n1'), 'meant': None},
        {**record('n1'), 'proposed': 'k1'},
        {**record('n1'), 'extra': 1},
    ],
)
async def test_a_partly_damaged_record_is_a_damaged_file(voice, damage):
    stored = json.dumps({'k1': damage}).encode()
    voice.writer.files[REJECTIONS_FILE] = stored

    response = await post(voice, 'reject', {'keys': ['k2']})

    assert response.status_code == HTTPStatus.CONFLICT
    assert voice.writer.files[REJECTIONS_FILE] == stored
    assert (await report(voice))['rejected'] == []
    assert (await post(voice, 'confirm', {'keys': ['k2']})).status_code == (
        HTTPStatus.CONFLICT
    )


async def test_a_damaged_rejections_file_stops_a_revision_before_it_is_saved(voice):
    voice.writer.files[REJECTIONS_FILE] = b'[]'

    async with api(voice) as client:
        response = await client.post(
            f'/api/input-collections/{UPLOAD}/inputs/n1/text',
            params={'collection_entry_id': COLLECTION},
            json={'text': 'time 1 is 1300'},
        )

    assert response.status_code == HTTPStatus.CONFLICT
    assert voice.inputs[0].text == TEXT
