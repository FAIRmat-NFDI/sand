"""The edit job's endpoints against a fake NOMAD: start (with the
running-job guard), status, per-input reports."""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

import pytest
from nomad_fakes import COLLECTION, UPLOAD, USER, FakeVoice, api, note

from sand.apis.routers import input_collections
from sand.apis.routers.input_collections import STALE_JOB_S
from sand.hysprint.sheet import EXTRACTION_STATUS_MAINFILE
from sand.services.nomad_api import NomadAPIError
from sand.sheet_edit.session import ORIGINAL_FILE, STATUS_FILE

pytestmark = pytest.mark.asyncio

EDIT_ACTION = 'sand.actions.edit:edit_action_entry_point'
EXTRACT_ACTION = 'sand.actions.extract:extract_action_entry_point'


@dataclass
class FakeStarter:
    started: list[tuple[str, object]] = field(default_factory=list)
    failing: bool = False

    async def __call__(self, action_id, data):
        if self.failing:
            raise RuntimeError('temporal unreachable')
        self.started.append((action_id, data))
        return f'job-{len(self.started)}'


@pytest.fixture
def voice(synthetic_xlsx) -> FakeVoice:
    voice = FakeVoice()
    voice.writer.files[ORIGINAL_FILE] = synthetic_xlsx
    voice.inputs = [note('n1', 'speed 4000')]
    return voice


@pytest.fixture
def starter(monkeypatch) -> FakeStarter:
    starter = FakeStarter()
    monkeypatch.setattr(input_collections, '_start_action', starter)
    return starter


async def start(voice, job='edit-async'):
    async with api(voice) as client:
        return await client.post(
            f'/api/input-collections/{UPLOAD}/{job}',
            params={'collection_entry_id': COLLECTION},
        )


async def get(voice, path):
    async with api(voice) as client:
        return await client.get(
            f'/api/input-collections/{UPLOAD}/{path}',
            params={'collection_entry_id': COLLECTION},
        )


def status_file(voice) -> dict:
    return json.loads(voice.writer.files[STATUS_FILE])


def put_status(voice, phase, collection=COLLECTION, age_s=0, **fields):
    at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    voice.writer.files[STATUS_FILE] = json.dumps(
        {
            'job_id': 'job-0',
            'phase': phase,
            'collection_entry_id': collection,
            'updated_at': at.isoformat(),
            'inputs': [],
            **fields,
        }
    ).encode()


async def test_start_reserves_the_status_and_starts_the_action(voice, starter):
    response = await start(voice)

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json() == {'job_id': 'job-1'}
    [(action_id, data)] = starter.started
    assert action_id == EDIT_ACTION
    assert (data.upload_id, data.user_id, data.collection_entry_id) == (
        UPLOAD,
        USER,
        COLLECTION,
    )
    # the reservation guards the window until the workflow's first snapshot
    reserved = status_file(voice)
    assert reserved['phase'] == 'starting'
    assert reserved['collection_entry_id'] == COLLECTION
    assert reserved['inputs'] == []
    assert datetime.fromisoformat(reserved['updated_at']).tzinfo is not None


@pytest.mark.parametrize('phase', ['starting', 'collecting', 'interpreting'])
async def test_a_running_job_refuses_a_second_start(voice, starter, phase):
    put_status(voice, phase)

    response = await start(voice)

    assert response.status_code == HTTPStatus.CONFLICT
    assert 'already running' in response.json()['detail']
    assert starter.started == []
    assert status_file(voice)['phase'] == phase  # untouched


@pytest.mark.parametrize(
    'phase, age_s',
    [('completed', 0), ('failed', 0), ('interpreting', STALE_JOB_S + 1)],
)
async def test_a_finished_or_stale_job_does_not_block(voice, starter, phase, age_s):
    put_status(voice, phase, age_s=age_s)

    response = await start(voice)

    assert response.status_code == HTTPStatus.OK, response.text
    assert len(starter.started) == 1


async def test_another_collections_running_job_blocks_too(voice, starter):
    # the sheet, the status and the final file are the upload's
    put_status(voice, 'interpreting', collection='collection-2')

    response = await start(voice)

    assert response.status_code == HTTPStatus.CONFLICT
    assert starter.started == []


async def test_another_collections_finished_job_does_not_block(voice, starter):
    put_status(voice, 'completed', collection='collection-2')

    response = await start(voice)

    assert response.status_code == HTTPStatus.OK, response.text
    assert status_file(voice)['collection_entry_id'] == COLLECTION


async def test_an_unreadable_updated_at_does_not_block(voice, starter):
    put_status(voice, 'interpreting', updated_at='yesterday')

    response = await start(voice)

    assert response.status_code == HTTPStatus.OK, response.text


async def test_two_starts_at_once_give_one_job(voice, starter):
    responses = await asyncio.gather(start(voice), start(voice))

    assert sorted(r.status_code for r in responses) == [
        HTTPStatus.OK,
        HTTPStatus.CONFLICT,
    ]
    assert len(starter.started) == 1


async def test_no_uploaded_sheet(voice, starter):
    del voice.writer.files[ORIGINAL_FILE]

    response = await start(voice)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()['detail'] == (
        'no sheet was uploaded for editing in this experiment'
    )
    assert starter.started == []
    assert STATUS_FILE not in voice.writer.files


async def test_no_inputs(voice, starter):
    voice.inputs = []

    response = await start(voice)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()['detail'] == 'nothing to apply: add an update first'
    assert starter.started == []


async def test_an_input_still_transcribing(voice, starter):
    voice.inputs.append(note('a1', None))

    response = await start(voice)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()['detail'] == (
        '1 input(s) not transcribed or processed yet; try again in a moment'
    )
    assert starter.started == []
    assert STATUS_FILE not in voice.writer.files


async def test_a_start_that_fails_releases_the_reservation(voice, starter):
    starter.failing = True

    response = await start(voice)

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()['detail'] == (
        'could not start the edit job: temporal unreachable'
    )
    released = status_file(voice)
    assert released['phase'] == 'failed'
    assert released['error'] == 'could not start the edit job: temporal unreachable'
    # and the next start goes through
    starter.failing = False
    assert (await start(voice)).status_code == HTTPStatus.OK


async def test_a_release_that_fails_still_reports_the_start_error(voice, starter):
    starter.failing = True
    voice.writer.writes_allowed = 1  # the reservation, then NOMAD is down

    response = await start(voice)

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()['detail'] == (
        'could not start the edit job: temporal unreachable'
    )
    assert status_file(voice)['phase'] == 'starting'  # not released


async def test_nomad_error_while_checking(voice, starter):
    async def refuse(client, upload_id, name):
        raise NomadAPIError(404, 'Upload upload-1 does not exist', step='upload_status')

    voice.writer.read_json = refuse

    response = await start(voice)

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert starter.started == []


async def test_nomad_error_while_collecting_writes_no_reservation(voice, starter):
    async def refuse(client, upload_id, collection_entry_id):
        raise NomadAPIError(400, 'not an input collection', step='collect_inputs')

    voice.collect_inputs = refuse

    response = await start(voice)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert starter.started == []
    assert STATUS_FILE not in voice.writer.files


async def test_extract_start_reserves_its_own_status(voice, starter):
    response = await start(voice, 'extract-async')

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json() == {'job_id': 'job-1'}
    [(action_id, data)] = starter.started
    assert action_id == EXTRACT_ACTION
    assert (data.upload_id, data.user_id, data.collection_entry_id) == (
        UPLOAD,
        USER,
        COLLECTION,
    )
    reserved = json.loads(voice.writer.files[EXTRACTION_STATUS_MAINFILE])
    assert (reserved['phase'], reserved['steps']) == ('starting', [])
    assert STATUS_FILE not in voice.writer.files


async def test_status_is_the_file_for_this_collection(voice):
    put_status(voice, 'interpreting', inputs_done=1, inputs_total=2)

    response = await get(voice, 'edit-status')

    assert response.status_code == HTTPStatus.OK
    assert response.json()['phase'] == 'interpreting'
    assert response.json()['inputs_done'] == 1


@pytest.mark.parametrize('path', ['edit-status', 'edits'])
async def test_no_status_or_another_collections_is_404(voice, path):
    assert (await get(voice, path)).status_code == HTTPStatus.NOT_FOUND
    put_status(voice, 'completed', collection='collection-2')
    assert (await get(voice, path)).status_code == HTTPStatus.NOT_FOUND


async def test_reports_are_the_inputs_of_the_status(voice):
    report = {
        'input': 1,
        'entry_id': 'n1',
        'text': 'speed 4000',
        'preview': '2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, samples 1–3',
        'ready': [{'key': 'k1', 'line': '...'}],
        'confirmed': [],
        'cells_written': 0,
        'refused': [],
    }
    put_status(voice, 'interpreting', inputs=[report], inputs_done=1, inputs_total=2)

    response = await get(voice, 'edits')

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        'job_id': 'job-0',
        'phase': 'interpreting',
        'inputs': [{**report, 'confirmed_since': []}],
    }
