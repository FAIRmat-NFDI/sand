"""The time of an update is when it was made: no reordering in an
experiment that started from a sheet."""

from http import HTTPStatus

import pytest
from nomad_fakes import COLLECTION, UPLOAD, FakeVoice, api

from sand.sheet_edit.session import ORIGINAL_FILE

pytestmark = pytest.mark.asyncio


async def move(voice):
    async with api(voice) as client:
        return await client.post(
            f'/api/input-collections/{UPLOAD}/inputs/n1/datetime',
            params={'collection_entry_id': COLLECTION},
            json={'datetime': '2026-10-31T10:00:00+00:00'},
        )


async def test_an_update_of_a_sheet_keeps_its_time(synthetic_xlsx):
    voice = FakeVoice()
    voice.writer.files[ORIGINAL_FILE] = synthetic_xlsx

    response = await move(voice)

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()['detail'] == (
        'the time of an update is when it was made; it cannot be changed'
    )
    assert voice.moved == []


async def test_other_experiments_still_reorder():
    voice = FakeVoice()

    response = await move(voice)

    assert response.status_code == HTTPStatus.OK, response.text
    assert voice.moved == [('n1', '2026-10-31T10:00:00+00:00')]
