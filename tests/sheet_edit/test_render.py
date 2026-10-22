import datetime
import re
from pathlib import Path

from synthetic_sheet import edited, letter

from sand.sheet_edit.catalog import build
from sand.sheet_edit.render import table

GOLDEN = Path(__file__).parent / 'golden' / 'synthetic_table.md'
INFO = 'Experiment Info'
SPIN2 = '2: Spin Coating'
CSS = '5: Close Space Sublimation'
TABLE_CELLS = 4
REAL_KIT_COLUMNS = 155


def cells(line: str) -> list[str]:
    """A table row's cells, split at '|' that are not escaped."""
    return [c.strip() for c in re.split(r'(?<!\\)\|', line)[1:-1]]


def value(text: str, section: str, header: str) -> str:
    """The Current value cell of one column's line."""
    line = next(
        line for line in text.splitlines() if f'| {section} | {header} |' in line
    )
    return cells(line)[3]


def rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith('| C')]


def test_synthetic_sheet_matches_the_golden_file(synthetic_xlsx):
    assert table(build(synthetic_xlsx)) == GOLDEN.read_text()


def test_values_with_separators_stay_one_unambiguous_cell(synthetic_xlsx):
    material, notes = letter(SPIN2, 'Material name'), letter(SPIN2, 'Notes')

    def change(ws):
        ws[f'{material}3'] = 'Cs0.05(MA0.17FA0.83)0.95Pb(I0.83Br0.17)3, x+y'
        ws[f'{notes}3'] = 'said "fast" | then\nslow'

    text = table(build(edited(synthetic_xlsx, change)))
    assert value(text, SPIN2, 'Material name') == (
        '"Cs0.05(MA0.17FA0.83)0.95Pb(I0.83Br0.17)3, x+y" samples 1; '
        '"Me-4PACz" samples 2–5, 7–13'
    )
    assert value(text, SPIN2, 'Notes') == (
        r'"said \"fast\" \| then\nslow" samples 1; (empty) samples 2–5, 7–13'
    )
    assert all(len(cells(line)) == TABLE_CELLS for line in rows(text))


def test_numbers_and_dates_are_shown_as_excel_shows_them(synthetic_xlsx):
    date, area = letter(INFO, 'Date'), letter(INFO, 'Sample area [cm^2]')

    def change(ws):
        for r in range(3, 15):
            ws[f'{date}{r}'] = datetime.date(2026, 9, 14)
            ws[f'{area}{r}'] = 3000.0

    text = table(build(edited(synthetic_xlsx, change)))
    assert value(text, INFO, 'Date') == '"2026-09-14T00:00:00"'
    assert value(text, INFO, 'Sample area [cm^2]') == '"3000"'


def test_headers_only_template_renders_empty(synthetic_xlsx):
    text = table(build(edited(synthetic_xlsx, lambda ws: ws.delete_rows(3, 100))))
    assert {cells(line)[3] for line in rows(text)} == {
        '(empty)',
        'the sample labels, see Samples below',
    }
    assert text.endswith('Samples: none\n')


def test_subbatches_restarting_at_1_are_listed_per_subbatch(synthetic_xlsx):
    subbatch, sample = letter(INFO, 'Subbatch'), letter(INFO, 'Sample')

    def second_subbatch(ws):
        for i, r in enumerate(range(9, 15)):
            ws[f'{subbatch}{r}'] = 1
            ws[f'{sample}{r}'] = i + 1

    text = table(build(edited(synthetic_xlsx, second_subbatch)))
    assert text.endswith(
        'Samples: 1–5, 7 of subbatch 0, 1–6 of subbatch 1 (12 samples)\n'
    )
    assert value(text, CSS, 'Substrate temperature [°C]') == (
        '"120" samples 1–5, 7 of subbatch 0; "115" samples 1–6 of subbatch 1'
    )


def test_real_kit_sheet(real_kit_xlsx):
    text = table(build(real_kit_xlsx))
    assert len(rows(text)) == REAL_KIT_COLUMNS
    assert all(len(cells(line)) == TABLE_CELLS for line in rows(text))
    assert value(text, CSS, 'Substrate temperature [°C]') == (
        '"120" samples 1–21; "115" samples 23–43'
    )
    assert text.endswith('Samples: 1–21, 23–43 (42 samples)\n')
