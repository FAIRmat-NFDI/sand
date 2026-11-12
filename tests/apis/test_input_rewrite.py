"""A written update can be revised or deleted: its confirmations go,
the next job reads it and the updates after it again."""

import json
from http import HTTPStatus

import pytest
from nomad_fakes import COLLECTION, UPLOAD, FakeVoice, api, note

from sand.apis.routers import input_collections
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
    """records: key -> input entry id"""
    voice.writer.files[CONFIRMATIONS_FILE] = json.dumps(
        {
            key: {'by': 'user', 'at': '2026-10-28T12:16:05+00:00', 'input': entry_id}
            for key, entry_id in records.items()
        }
    ).encode()


def confirmations(voice):
    return json.loads(voice.writer.files[CONFIRMATIONS_FILE])


async def revise(voice, entry_id='n1', text='time 1 is 1100'):
    async with api(voice) as client:
        return await client.post(
            f'/api/input-collections/{UPLOAD}/inputs/{entry_id}/text',
            params={'collection_entry_id': COLLECTION},
            json={'text': text},
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
    voice.inputs = [note('n1', 'time 1 is 1000'), note('n2', 'speed 4000')]
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


async def test_confirmations_stay_when_the_change_is_refused(voice, monkeypatch):
    # not this collection's input, an empty note (the fake does not refuse,
    # the service does), an input the experiment needs
    put_confirmations(voice, {'k1': 'n1', 'k7': 'n7'})

    await revise(voice, 'n7')
    await revise(voice, 'n1', text='  ')
    await delete(voice, 'n7')
    monkeypatch.setattr(input_collections, '_undeletable_entry_ids', lambda _: ('n1',))
    await delete(voice, 'n1')

    assert sorted(confirmations(voice)) == ['k1', 'k7']


async def test_an_unconfirmed_update_leaves_the_file_alone(voice):
    assert (await revise(voice)).status_code == HTTPStatus.OK
    assert CONFIRMATIONS_FILE not in voice.writer.files


async def test_a_damaged_confirmations_file_is_not_rewritten(voice):
    voice.writer.files[CONFIRMATIONS_FILE] = b'not json'

    response = await revise(voice)

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'].startswith('confirmations file damaged')
    assert voice.writer.files[CONFIRMATIONS_FILE] == b'not json'
    # refused before anything changed
    assert voice.revised == []
    assert (await delete(voice)).status_code == HTTPStatus.CONFLICT
    assert voice.removed == []


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

    assert [r['confirm_again'] for r in reports] == [0, 1]


async def test_an_update_that_lost_its_edit_is_told_too(voice):
    # n1 changed and n2's edit is refused now
    put_status(
        voice,
        [
            {
                'input': 2,
                'entry_id': 'n2',
                'ready': [],
                'confirmed': [],
                'not_applied': 1,
            }
        ],
    )
    put_confirmations(voice, {'k5': 'n2'})

    (report,) = (await status(voice))['inputs']

    assert report['confirm_again'] == 1


async def test_an_update_the_sheet_already_holds_is_not_told(voice):
    # n1 changed; n2's value is in the sheet already: nothing to do
    put_status(
        voice,
        [{'input': 2, 'entry_id': 'n2', 'ready': [], 'confirmed': [], 'unchanged': 1}],
    )
    put_confirmations(voice, {'k5': 'n2'})

    (report,) = (await status(voice))['inputs']

    assert report['confirm_again'] == 0


async def test_a_withdrawn_confirmation_waits_for_the_next_run(voice):
    put_status(
        voice, [{'input': 1, 'entry_id': 'n1', 'ready': READY, 'confirmed': ['k1']}]
    )
    put_confirmations(voice, {'k1': 'n1'})

    assert (await revise(voice)).status_code == HTTPStatus.OK
    (report,) = (await status(voice))['inputs']

    assert report['confirmed'] == []
    assert report['unconfirmed_since'] == ['k1']


async def test_a_partly_confirmed_update_is_not_to_confirm_again(voice):
    put_confirmations(voice, {'k1': 'n1'})

    (report,) = (await status(voice))['inputs']

    assert report['confirmed'] == ['k1']
    assert report['confirm_again'] == 0


async def test_the_inputs_list_says_which_are_applied(voice):
    voice.inputs = [
        note('n1', 'time 1 is 1000'),
        note('n2', 'speed 4000'),
        note('n3', 'speed 10 percent higher'),
    ]
    # n3's confirmation k5 matches none of its edits now
    put_status(
        voice,
        [
            {'input': 1, 'entry_id': 'n1', 'ready': READY, 'confirmed': ['k1']},
            {'input': 3, 'entry_id': 'n3', 'ready': [{'key': 'k6'}], 'confirmed': []},
        ],
    )
    put_confirmations(voice, {'k1': 'n1', 'k5': 'n3'})

    async with api(voice) as client:
        response = await client.get(
            f'/api/input-collections/{UPLOAD}/inputs',
            params={'collection_entry_id': COLLECTION},
        )

    assert response.status_code == HTTPStatus.OK
    assert [(i['entry_id'], i['applied']) for i in response.json()['inputs']] == [
        ('n1', True),
        ('n2', False),
        ('n3', False),
    ]


async def test_a_confirmation_is_its_own_inputs_when_two_share_a_key(voice):
    # n1 and n2 say the same on the same sheet: one key, n2 confirmed
    same = [{'key': 'k1', 'line': '...'}]
    put_status(
        voice,
        [
            {'input': 1, 'entry_id': 'n1', 'ready': same, 'confirmed': []},
            {'input': 2, 'entry_id': 'n2', 'ready': same, 'confirmed': []},
        ],
    )
    put_confirmations(voice, {'k1': 'n2'})

    reports = (await status(voice))['inputs']

    assert [r['confirmed_since'] for r in reports] == [[], ['k1']]


async def test_no_input_is_applied_by_another_collections_job(voice):
    voice.inputs = [note('n1', 'time 1 is 1000')]
    put_status(voice, [], collection='collection-2')
    put_confirmations(voice, {'k1': 'n1'})

    async with api(voice) as client:
        response = await client.get(
            f'/api/input-collections/{UPLOAD}/inputs',
            params={'collection_entry_id': COLLECTION},
        )

    assert [i['applied'] for i in response.json()['inputs']] == [False]


async def test_a_carried_edit_counts_as_confirmed(voice):
    # n2's k6 was written by the job carrying k5 (the same change confirmed
    # on the sheet before n1 was written)
    carried = {
        'input': 2,
        'entry_id': 'n2',
        'ready': [{'key': 'k6', 'line': '...'}],
        'confirmed': ['k6'],
        'carried': {'k6': 'k5'},
    }
    put_status(voice, [carried])
    put_confirmations(voice, {'k5': 'n2'})

    (report,) = (await status(voice))['inputs']

    assert report['confirmed'] == ['k6']
    assert report['unconfirmed_since'] == []
    assert report['confirm_again'] == 0


async def test_a_carried_edit_goes_with_its_confirmation(voice):
    # n2 revised: k5 is removed, the sheet must lose k6
    put_status(
        voice,
        [
            {
                'input': 2,
                'entry_id': 'n2',
                'ready': [{'key': 'k6', 'line': '...'}],
                'confirmed': ['k6'],
                'carried': {'k6': 'k5'},
            }
        ],
    )
    put_confirmations(voice, {})

    (report,) = (await status(voice))['inputs']

    assert report['unconfirmed_since'] == ['k6']
