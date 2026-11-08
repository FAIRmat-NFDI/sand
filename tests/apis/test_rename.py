"""PATCH /input-collections/{upload}/name against a fake NOMAD."""

from http import HTTPStatus

import pytest
from nomad_fakes import COLLECTION, UPLOAD, FakeVoice, api

pytestmark = pytest.mark.asyncio


@pytest.fixture
def voice() -> FakeVoice:
    return FakeVoice()


async def rename(voice, name, token='tok', collection=COLLECTION):
    async with api(voice, token) as client:
        return await client.patch(
            f'/api/input-collections/{UPLOAD}/name',
            params={'collection_entry_id': collection},
            json={'name': name},
        )


async def test_rename_gives_the_list_entry_under_its_new_name(voice):
    response = await rename(voice, '  batch   8 ')

    assert response.status_code == HTTPStatus.OK, response.text
    # no `created`, not even null: the page keeps the one the list gave
    assert response.json() == {
        'upload_id': UPLOAD,
        'entry_id': COLLECTION,
        'entry_url': f'http://nomad/{UPLOAD}/{COLLECTION}',
        'name': 'batch 8',
    }
    # one line, single-spaced: the name as the page and NOMAD show it
    assert voice.renamed == ['batch 8']


@pytest.mark.parametrize('name', ['', '   ', '\n\t'])
async def test_a_blank_name_is_refused_before_nomad_is_asked(voice, name):
    response = await rename(voice, name)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()['detail'] == 'Give the sheet a name'
    assert voice.renamed == []


async def test_nomad_down_is_reported(voice):
    voice.unrenamable = True

    response = await rename(voice, 'batch 8')

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert 'NOMAD is down' in response.json()['detail']


async def test_not_logged_in(voice):
    response = await rename(voice, 'batch 8', token=None)

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert voice.renamed == []
