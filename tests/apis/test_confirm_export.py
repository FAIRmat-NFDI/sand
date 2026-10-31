"""Confirming edits, downloading the final sheet, reading the changelog,
against a fake NOMAD."""

import asyncio
import json
from datetime import datetime, timezone
from http import HTTPStatus

import pytest
from nomad_fakes import COLLECTION, UPLOAD, USER, USERNAME, FakeVoice, api

from sand.services.nomad_api import XLSX_CONTENT_TYPE
from sand.sheet_edit import changelog
from sand.sheet_edit.session import CHANGELOG_FILE, CONFIRMATIONS_FILE, STATUS_FILE
from sand.sheet_edit.workbook import sha256

pytestmark = pytest.mark.asyncio

READY = [{'key': 'k1', 'line': 'speed 3000 → 4000'}, {'key': 'k2', 'line': '...'}]


@pytest.fixture
def voice() -> FakeVoice:
    return FakeVoice()


def put_status(voice, phase='completed', collection=COLLECTION, **fields):
    voice.writer.files[STATUS_FILE] = json.dumps(
        {
            'job_id': 'job-0',
            'phase': phase,
            'collection_entry_id': collection,
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'inputs': [{'input': 1, 'ready': READY, 'confirmed': []}],
            **fields,
        }
    ).encode()


def put_final(voice, xlsx: bytes, **status):
    """A completed job whose final sheet is the original: an empty log."""
    log = changelog.changelog(xlsx, [], {}, 'gemini/test v1')
    voice.writer.files[CHANGELOG_FILE] = changelog.to_json(log).encode()
    voice.writer.files['sheet_edit/final.xlsx.orig'] = xlsx
    put_status(voice, final=sha256(xlsx), **status)


def confirmations(voice) -> dict:
    return json.loads(voice.writer.files[CONFIRMATIONS_FILE])


async def confirm(voice, keys):
    async with api(voice) as client:
        return await client.post(
            f'/api/input-collections/{UPLOAD}/edits/confirm',
            params={'collection_entry_id': COLLECTION},
            json={'keys': keys},
        )


async def get(voice, path, **params):
    async with api(voice) as client:
        return await client.get(
            f'/api/input-collections/{UPLOAD}/{path}',
            params={'collection_entry_id': COLLECTION, **params},
        )


# --- confirm ----------------------------------------------------------------------


async def test_confirm_records_who_and_when(voice):
    put_status(voice, 'completed')

    response = await confirm(voice, ['k1'])

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json() == {'confirmed': ['k1']}
    [(key, record)] = confirmations(voice).items()
    assert key == 'k1'
    assert record['by'] == USERNAME
    at = datetime.fromisoformat(record['at'])
    assert at.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - at).total_seconds()) < 60  # noqa: PLR2004


async def test_a_confirmed_key_keeps_its_first_record(voice):
    put_status(voice, 'completed')
    voice.writer.files[CONFIRMATIONS_FILE] = json.dumps(
        {'k1': {'by': 'user', 'at': '2026-10-28T12:16:05+00:00'}}
    ).encode()

    response = await confirm(voice, ['k1', 'k2'])

    assert response.json() == {'confirmed': ['k1', 'k2']}
    records = confirmations(voice)
    assert records['k1'] == {'by': 'user', 'at': '2026-10-28T12:16:05+00:00'}
    assert records['k2']['by'] == USERNAME


async def test_two_confirms_at_once_both_survive(voice):
    put_status(voice, 'completed')

    responses = await asyncio.gather(confirm(voice, ['k1']), confirm(voice, ['k2']))

    assert [r.status_code for r in responses] == [HTTPStatus.OK] * 2
    assert sorted(confirmations(voice)) == ['k1', 'k2']


async def test_by_falls_back_to_the_user_id(voice):
    put_status(voice, 'completed')
    voice.me = {'user_id': USER}

    await confirm(voice, ['k1'])

    assert confirmations(voice)['k1']['by'] == USER


async def test_a_failed_write_leaves_the_file_and_reports(voice):
    put_status(voice, 'completed')
    voice.writer.writes_allowed = 0

    response = await confirm(voice, ['k1'])

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert CONFIRMATIONS_FILE not in voice.writer.files


async def test_confirming_a_running_jobs_ready_edit_is_fine(voice):
    # confirmations are read once at a job's start: this lands in the next run
    put_status(voice, 'interpreting')

    assert (await confirm(voice, ['k1'])).status_code == HTTPStatus.OK


async def test_a_key_the_running_job_has_not_reported_yet_waits(voice):
    # the job starts with no reports; the page may still show the old ones
    put_status(voice, 'collecting', inputs=[])

    response = await confirm(voice, ['k1'])

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == (
        'an edit job is running; confirm when it has finished'
    )


async def test_only_a_ready_edit_of_the_last_job_can_be_confirmed(voice):
    put_status(voice, 'completed')

    response = await confirm(voice, ['k1', 'k9', 'k8'])

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()['detail'] == 'not a ready edit of the last job: k8, k9'
    assert CONFIRMATIONS_FILE not in voice.writer.files


async def test_no_keys(voice):
    put_status(voice, 'completed')

    assert (await confirm(voice, [])).status_code == HTTPStatus.BAD_REQUEST


async def test_no_job_yet(voice):
    assert (await confirm(voice, ['k1'])).status_code == HTTPStatus.NOT_FOUND


async def test_a_damaged_confirmations_file_is_never_overwritten(voice):
    put_status(voice, 'completed')
    voice.writer.files[CONFIRMATIONS_FILE] = b'{"k1": "yes"}'

    response = await confirm(voice, ['k1'])

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'].startswith('confirmations file damaged')
    assert voice.writer.files[CONFIRMATIONS_FILE] == b'{"k1": "yes"}'


# --- final sheet ------------------------------------------------------------------


async def test_the_final_sheet_downloads_under_the_experiments_name(
    voice, synthetic_xlsx
):
    put_final(voice, synthetic_xlsx)

    response = await get(voice, 'final-sheet')

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.content == synthetic_xlsx
    assert response.headers['content-type'] == XLSX_CONTENT_TYPE
    assert response.headers['content-disposition'] == (
        'attachment; filename="batch 7.xlsx"; filename*=UTF-8\'\'batch%207.xlsx'
    )


async def test_a_name_in_any_script_downloads(voice, synthetic_xlsx):
    put_final(voice, synthetic_xlsx)
    voice.name = 'Batch 7 – 测试 "x"'

    response = await get(voice, 'final-sheet')

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.headers['content-disposition'] == (
        'attachment; filename="Batch 7 _ __ _x_.xlsx"; '
        "filename*=UTF-8''Batch%207%20%E2%80%93%20%E6%B5%8B%E8%AF%95%20%22x%22.xlsx"
    )


@pytest.mark.parametrize('phase', ['starting', 'interpreting', 'writing-sheet'])
async def test_no_download_while_a_job_runs(voice, synthetic_xlsx, phase):
    put_final(voice, synthetic_xlsx, phase=phase)

    response = await get(voice, 'final-sheet')

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == 'an edit job is running; wait for it'


async def test_no_download_after_a_failed_job(voice, synthetic_xlsx):
    put_final(voice, synthetic_xlsx, phase='failed', error='provider down')

    response = await get(voice, 'final-sheet')

    assert response.status_code == HTTPStatus.CONFLICT
    assert 'did not complete' in response.json()['detail']


async def test_a_sheet_that_is_not_the_jobs_is_refused(voice, synthetic_xlsx):
    put_final(voice, synthetic_xlsx)
    voice.writer.files['sheet_edit/final.xlsx.orig'] = synthetic_xlsx + b'x'

    response = await get(voice, 'final-sheet')

    assert response.status_code == HTTPStatus.CONFLICT
    assert 'not the one the last job recorded' in response.json()['detail']


async def test_no_final_sheet_stored(voice, synthetic_xlsx):
    put_final(voice, synthetic_xlsx)
    del voice.writer.files['sheet_edit/final.xlsx.orig']

    assert (await get(voice, 'final-sheet')).status_code == HTTPStatus.NOT_FOUND


# --- changelog --------------------------------------------------------------------


async def test_changelog_as_text_and_as_json(voice, synthetic_xlsx):
    put_final(voice, synthetic_xlsx)

    text = await get(voice, 'changelog')
    assert text.status_code == HTTPStatus.OK, text.text
    assert text.headers['content-type'].startswith('text/plain')
    assert f'original  sha256 {sha256(synthetic_xlsx)}' in text.text

    as_json = await get(voice, 'changelog', format='json')
    assert as_json.status_code == HTTPStatus.OK
    assert as_json.content == voice.writer.files[CHANGELOG_FILE]
    assert changelog.from_json(as_json.text).final == sha256(synthetic_xlsx)


async def test_changelog_formats(voice, synthetic_xlsx):
    put_final(voice, synthetic_xlsx)

    assert (await get(voice, 'changelog', format='xml')).status_code == (
        HTTPStatus.BAD_REQUEST
    )


async def test_a_damaged_changelog_is_not_served(voice, synthetic_xlsx):
    put_final(voice, synthetic_xlsx)
    damaged = voice.writer.files[CHANGELOG_FILE].replace(b'"entries"', b'"entry"')
    voice.writer.files[CHANGELOG_FILE] = damaged

    response = await get(voice, 'changelog')

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'].startswith('changelog damaged')


async def test_a_changelog_of_another_job_is_not_served(voice, synthetic_xlsx):
    put_final(voice, synthetic_xlsx)
    put_status(voice, final='0' * 64)

    response = await get(voice, 'changelog')

    assert response.status_code == HTTPStatus.CONFLICT
    assert "not the last job's" in response.json()['detail']


@pytest.mark.parametrize('phase', ['interpreting', 'failed'])
async def test_no_changelog_while_running_or_after_failure(
    voice, synthetic_xlsx, phase
):
    put_final(voice, synthetic_xlsx, phase=phase)

    response = await get(voice, 'changelog')

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] in (
        'an edit job is running; wait for it',
        'the last edit job did not complete; run it again',
    )


async def test_no_changelog_stored(voice, synthetic_xlsx):
    put_final(voice, synthetic_xlsx)
    del voice.writer.files[CHANGELOG_FILE]

    assert (await get(voice, 'changelog')).status_code == HTTPStatus.NOT_FOUND
