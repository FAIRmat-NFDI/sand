"""GET .../keyterms and the relay's choice of terms."""

from http import HTTPStatus

import pytest
from nomad_fakes import COLLECTION, UPLOAD, FakeVoice, api
from synthetic_sheet import edited, letter

from sand.apis.routers.live_transcript import MAX_KEYTERM_LENGTH, keyterms
from sand.sheet_edit.session import FINAL_FILE, ORIGINAL_FILE

MATERIAL = letter('2: Spin Coating', 'Material name')


async def get_terms(voice):
    async with api(voice) as client:
        return await client.get(
            f'/api/input-collections/{UPLOAD}/keyterms',
            params={'collection_entry_id': COLLECTION},
        )


@pytest.mark.asyncio
async def test_the_terms_are_the_uploaded_sheets_not_the_edited_ones(synthetic_xlsx):
    voice = FakeVoice()
    voice.writer.files[ORIGINAL_FILE] = synthetic_xlsx
    voice.writer.files[FINAL_FILE] = edited(
        synthetic_xlsx, lambda ws: ws.__setitem__(f'{MATERIAL}3', 'NiOx')
    )

    response = await get_terms(voice)

    assert response.status_code == HTTPStatus.OK, response.text
    terms = response.json()['keyterms']
    assert 'Me-4PACz' in terms
    assert 'NiOx' not in terms


@pytest.mark.asyncio
async def test_no_uploaded_sheet_is_not_found():
    response = await get_terms(FakeVoice())

    assert response.status_code == HTTPStatus.NOT_FOUND


def test_the_sheets_terms_go_before_the_labs():
    assert keyterms(['PbI2', 'Ossila'], ['ossila', 'Pero2']) == [
        'PbI2',
        'Ossila',
        'Pero2',
    ]


def test_terms_off_in_the_config_are_off_for_the_sheet_too():
    assert keyterms(['PbI2'], []) == []


def test_what_the_browser_sends_is_kept_in_bounds():
    sent = [
        '  Pb   I2 ',
        '',
        'x' * (MAX_KEYTERM_LENGTH + 1),
        *[f't{i}' for i in range(200)],
    ]

    terms = keyterms(sent, ['Pero2'])

    assert terms[0] == 'Pb I2'
    assert '' not in terms
    assert all(len(t) <= MAX_KEYTERM_LENGTH for t in terms)
    assert terms[-1] == 'Pero2'
