"""GET /input-collections/{upload}/sheet-view against a fake NOMAD."""

from http import HTTPStatus

import pytest
from nomad_fakes import COLLECTION, UPLOAD, FakeVoice, api
from synthetic_sheet import edited, letter

from sand.sheet_edit import changelog
from sand.sheet_edit.session import CHANGELOG_FILE, FINAL_FILE, ORIGINAL_FILE
from sand.sheet_edit.workbook import sha256

pytestmark = pytest.mark.asyncio

SPEED = letter('2: Spin Coating', 'Rotation speed [rpm]')
ROWS = range(3, 15)


@pytest.fixture
def voice() -> FakeVoice:
    return FakeVoice()


async def get_view(voice):
    async with api(voice) as client:
        return await client.get(
            f'/api/input-collections/{UPLOAD}/sheet-view',
            params={'collection_entry_id': COLLECTION},
        )


def speed_entries(rows=ROWS) -> tuple[changelog.Entry, ...]:
    return tuple(
        changelog.Entry(
            input=1,
            cell=f'{SPEED}{r}',
            row=r,
            column=25,
            section='2: Spin Coating',
            header='Rotation speed [rpm]',
            sample=str(r - 2),
            old=3000,
            new=4000,
            said='speed 4000',
            confirmed_by='tester',
            confirmed_at='2026-11-05T10:00:00+00:00',
        )
        for r in rows
    )


def put_edited(voice, original: bytes, *, log_final: str | None = None) -> bytes:
    """A final sheet with the speed at 4000 and its changelog; `log_final`
    forges the hash the log names."""

    def change(ws):
        for r in ROWS:
            ws[f'{SPEED}{r}'] = 4000

    final = edited(original, change)
    log = changelog.Changelog(
        sheet='Experiment Data',
        original=sha256(original),
        final=log_final or sha256(final),
        model='test',
        entries=speed_entries(),
    )
    voice.writer.files[FINAL_FILE] = final
    voice.writer.files[CHANGELOG_FILE] = changelog.to_json(log).encode()
    return final


async def test_the_uploaded_sheet_before_any_edit(voice, synthetic_xlsx):
    voice.writer.files[ORIGINAL_FILE] = synthetic_xlsx

    response = await get_view(voice)

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert (body['source'], body['sha256']) == ('uploaded', sha256(synthetic_xlsx))
    assert body['sheet'] == 'Experiment Data'
    assert body['info']['samples'] == len(ROWS)
    assert [s['position'] for s in body['steps']][:3] == [1, 2, 3]
    assert body['changed'] == []
    assert body['warnings'] == ['Sample 6 is missing', 'Step 7 (Spin Coating) is empty']
    # the page polls this: never held up by NOMAD processing the upload
    assert voice.writer.reads == [(ORIGINAL_FILE, False), (FINAL_FILE, False)]


async def test_the_edited_sheet_when_the_files_hold_together(voice, synthetic_xlsx):
    voice.writer.files[ORIGINAL_FILE] = synthetic_xlsx
    final = put_edited(voice, synthetic_xlsx)

    body = (await get_view(voice)).json()

    assert (body['source'], body['sha256']) == ('edited', sha256(final))
    spin = next(s for s in body['steps'] if s['position'] == 2)  # noqa: PLR2004
    speed = next(p for p in spin['params'] if p['header'] == 'Rotation speed [rpm]')
    assert speed['values'][0]['value'] == '4000'
    assert [(c['row'], c['old'], c['new']) for c in body['changed']] == [
        (r, '3000', '4000') for r in ROWS
    ]
    assert body['warnings'] == ['Sample 6 is missing', 'Step 7 (Spin Coating) is empty']
    assert [name for name, _ in voice.writer.reads] == [
        ORIGINAL_FILE,
        FINAL_FILE,
        CHANGELOG_FILE,
    ]
    assert not any(waited for _, waited in voice.writer.reads)


async def test_a_job_that_wrote_nothing_is_the_edited_sheet_unchanged(
    voice, synthetic_xlsx
):
    voice.writer.files[ORIGINAL_FILE] = synthetic_xlsx
    voice.writer.files[FINAL_FILE] = synthetic_xlsx
    log = changelog.changelog(synthetic_xlsx, [], {}, 'test')
    voice.writer.files[CHANGELOG_FILE] = changelog.to_json(log).encode()

    body = (await get_view(voice)).json()

    assert (body['source'], body['changed']) == ('edited', [])


@pytest.mark.parametrize(
    'spoil',
    [
        pytest.param(
            lambda v, x: put_edited(v, x, log_final='0' * 64),
            id='log-names-another-sheet',
        ),
        pytest.param(
            lambda v, x: v.writer.files.__setitem__(CHANGELOG_FILE, b'not json'),
            id='log-damaged',
        ),
        pytest.param(lambda v, x: v.writer.files.pop(CHANGELOG_FILE), id='log-missing'),
    ],
)
async def test_an_edited_sheet_that_cannot_be_vouched_for(voice, synthetic_xlsx, spoil):
    voice.writer.files[ORIGINAL_FILE] = synthetic_xlsx
    put_edited(voice, synthetic_xlsx)
    spoil(voice, synthetic_xlsx)

    body = (await get_view(voice)).json()

    assert (body['source'], body['sha256']) == ('uploaded', sha256(synthetic_xlsx))
    assert body['changed'] == []
    assert body['warnings'][-1] == (
        'the edited sheet stored with this experiment does not match its '
        'changelog; this is the uploaded sheet'
    )


async def test_an_edited_sheet_of_another_original_is_not_shown(voice, synthetic_xlsx):
    # the original replaced after the job: the log names the old one
    voice.writer.files[ORIGINAL_FILE] = synthetic_xlsx
    put_edited(voice, synthetic_xlsx)
    other = edited(synthetic_xlsx, lambda ws: ws.__setitem__(f'{SPEED}3', 2000))
    voice.writer.files[ORIGINAL_FILE] = other

    body = (await get_view(voice)).json()

    assert (body['source'], body['sha256']) == ('uploaded', sha256(other))


async def test_no_sheet(voice):
    response = await get_view(voice)

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()['detail'] == 'this experiment did not start from a sheet'


async def test_a_cell_edited_back_is_not_a_change(voice, synthetic_xlsx):
    voice.writer.files[ORIGINAL_FILE] = synthetic_xlsx
    voice.writer.files[FINAL_FILE] = synthetic_xlsx
    back = tuple(
        changelog.Entry(
            **{
                **e.__dict__,
                'input': 2,
                'old': 4000,
                'new': 3000,
                'said': 'speed back to 3000',
            }
        )
        for e in speed_entries()
    )
    log = changelog.Changelog(
        sheet='Experiment Data',
        original=sha256(synthetic_xlsx),
        final=sha256(synthetic_xlsx),
        model='test',
        entries=speed_entries() + back,
    )
    voice.writer.files[CHANGELOG_FILE] = changelog.to_json(log).encode()

    body = (await get_view(voice)).json()

    assert (body['source'], body['changed']) == ('edited', [])
