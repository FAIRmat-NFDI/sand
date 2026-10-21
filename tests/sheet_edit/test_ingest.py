from kit_like import SAMPLE_LABELS, edited, letter

from sand.sheet_edit.workbook import ingest_problems

INFO = 'Experiment Info'
CLEANING = '1: Cleaning UV-Ozone'
SPIN2 = '2: Spin Coating'
SPIN3 = '3: Spin Coating'
LAST_ROW = 2 + len(SAMPLE_LABELS)


def _set(cell: str, value):
    def change(ws):
        ws[cell] = value

    return change


def test_synthetic_sheet_is_accepted(kit_like_xlsx):
    assert ingest_problems(kit_like_xlsx) == []


def test_real_kit_sheet_is_accepted(real_kit_xlsx):
    assert ingest_problems(real_kit_xlsx) == []


def test_not_an_xlsx():
    [problem] = ingest_problems(b'not a spreadsheet')
    assert problem.startswith('not a readable .xlsx file')


def test_first_group_must_be_experiment_info(kit_like_xlsx):
    problems = ingest_problems(edited(kit_like_xlsx, _set('A1', 'Info')))
    # no info group, so no Nomad ID / Sample column either
    assert problems == [
        "cell A1 must be 'Experiment Info' (found 'Info')",
        "'Experiment Info' has no 'Nomad ID' column",
        "'Experiment Info' has no 'Sample' column",
    ]


def test_step_label_needs_a_position(kit_like_xlsx):
    cell = f'{letter(SPIN2, "Material name")}1'
    assert ingest_problems(edited(kit_like_xlsx, _set(cell, 'Spin Coating'))) == [
        f"step label 'Spin Coating' (cell {cell}) has no 'N:' position"
    ]


def test_step_position_must_be_unique(kit_like_xlsx):
    spin2 = letter(SPIN2, 'Material name')
    spin3 = letter(SPIN3, 'Material name')
    xlsx = edited(kit_like_xlsx, _set(f'{spin3}1', '2: Spin Coating'))
    assert ingest_problems(xlsx) == [
        f'step position 2 is used twice (cells {spin2}1 and {spin3}1)'
    ]


def test_values_without_a_header(kit_like_xlsx):
    area = letter(INFO, 'Sample area [cm^2]')
    assert ingest_problems(edited(kit_like_xlsx, _set(f'{area}2', None))) == [
        f'column {area} has values but no header in row 2'
    ]


def test_values_right_of_the_last_header(kit_like_xlsx):
    def change(ws):
        ws.cell(5, ws.max_column + 2, 'stray')

    [problem] = ingest_problems(edited(kit_like_xlsx, change))
    assert problem.endswith('has values but no header in row 2')


def test_header_unique_within_a_step(kit_like_xlsx):
    first, second = letter(CLEANING, 'Solvent 1'), letter(CLEANING, 'Solvent 2')
    xlsx = edited(kit_like_xlsx, _set(f'{second}2', 'Solvent 1'))
    assert ingest_problems(xlsx) == [
        f"'Solvent 1' appears twice in '{CLEANING}' (columns {first}, {second})"
    ]


def test_same_header_in_different_steps_is_fine(kit_like_xlsx):
    # 'Rotation speed [rpm]' is in spin coatings 2, 3 and 7
    assert ingest_problems(kit_like_xlsx) == []


def test_nomad_id_column_required(kit_like_xlsx):
    cell = f'{letter(INFO, "Nomad ID")}2'
    [problem, *_] = ingest_problems(edited(kit_like_xlsx, _set(cell, 'Lab ID')))
    assert problem == "'Experiment Info' has no 'Nomad ID' column"


def test_no_empty_row_between_samples(kit_like_xlsx):
    def change(ws):
        for c in range(1, ws.max_column + 1):
            ws.cell(6, c).value = None

    assert ingest_problems(edited(kit_like_xlsx, change)) == [
        'row 6 is empty between sample rows'
    ]


def test_every_sample_row_needs_a_sample(kit_like_xlsx):
    cell = f'{letter(INFO, "Sample")}5'
    assert ingest_problems(edited(kit_like_xlsx, _set(cell, None))) == [
        "row 5 has no 'Sample' value"
    ]


def test_duplicate_sample(kit_like_xlsx):
    cell = f'{letter(INFO, "Sample")}4'
    assert ingest_problems(edited(kit_like_xlsx, _set(cell, 1))) == [
        'sample 1 of subbatch 0 appears twice (rows 3 and 4)'
    ]


def test_samples_may_restart_in_another_subbatch(kit_like_xlsx):
    subbatch, sample = letter(INFO, 'Subbatch'), letter(INFO, 'Sample')

    def change(ws):
        for i, r in enumerate(range(9, LAST_ROW + 1)):
            ws[f'{subbatch}{r}'] = 1
            ws[f'{sample}{r}'] = i + 1

    assert ingest_problems(edited(kit_like_xlsx, change)) == []


def test_no_merged_cells_below_the_step_row(kit_like_xlsx):
    xlsx = edited(kit_like_xlsx, lambda ws: ws.merge_cells('B5:C5'))
    assert ingest_problems(xlsx) == [
        'merged cells B5:C5 below row 1: cells must be single to be edited'
    ]


def test_headers_without_samples(kit_like_xlsx):
    xlsx = edited(kit_like_xlsx, lambda ws: ws.delete_rows(3, LAST_ROW))
    assert ingest_problems(xlsx) == ['no sample rows below the headers']
