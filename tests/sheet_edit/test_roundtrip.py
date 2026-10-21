from synthetic_sheet import edited, letter, with_formula_results, with_part

from sand.sheet_edit.workbook import (
    CellChange,
    cell_changes,
    roundtrip_problems,
    workbook_problems,
)


def _set(cell: str, value):
    def change(ws):
        ws[cell] = value

    return change


def test_synthetic_sheet_survives_a_round_trip(synthetic_xlsx):
    assert roundtrip_problems(synthetic_xlsx) == []


def test_real_kit_sheet_loses_only_its_formula_results(real_kit_xlsx):
    # openpyxl keeps the formulas but drops Excel's cached results, which
    # are what NOMAD's parser reads: Nomad ID and Deposition Time
    assert roundtrip_problems(real_kit_xlsx) == [
        "'Experiment Data' column F: formula results lost on 42 rows (F3…F44)",
        "'Experiment Data' column CN: formula results lost on 42 rows (CN3…CN44)",
    ]


def test_lost_formula_results_are_reported_per_column(synthetic_xlsx):
    nomad_id = letter('Experiment Info', 'Nomad ID')
    cached = with_formula_results(
        synthetic_xlsx, {f'{nomad_id}3': 'KIT_a', f'{nomad_id}4': 'KIT_b'}
    )
    assert roundtrip_problems(cached) == [
        f"'Experiment Data' column {nomad_id}: formula results lost on 2 rows "
        f'({nomad_id}3…{nomad_id}4)'
    ]


def test_cell_changes_lists_value_type_and_format(synthetic_xlsx):
    speed = f'{letter("2: Spin Coating", "Rotation speed [rpm]")}3'

    def change(ws):
        ws[speed] = '3000'  # number -> text
        ws['H3'].number_format = '0.00%'

    after = edited(synthetic_xlsx, change)
    assert cell_changes(synthetic_xlsx, after) == [
        CellChange('Experiment Data', 'H3', 'number format', 'General', '0.00%'),
        CellChange('Experiment Data', speed, 'value', 3000, '3000'),
        CellChange('Experiment Data', speed, 'type', 'n', 's'),
    ]


def test_unchanged_files_have_no_cell_changes(synthetic_xlsx):
    assert cell_changes(synthetic_xlsx, synthetic_xlsx) == []


def test_renamed_sheet_is_reported(synthetic_xlsx):
    def change(ws):
        ws.title = 'Data'

    assert workbook_problems(synthetic_xlsx, edited(synthetic_xlsx, change)) == [
        "sheets changed: ['Experiment Data'] -> ['Data']"
    ]


def test_workbook_problems_reports_cell_and_merge_changes(synthetic_xlsx):
    def change(ws):
        ws['B3'] = 'Other'
        ws.unmerge_cells('A1:I1')

    assert workbook_problems(synthetic_xlsx, edited(synthetic_xlsx, change)) == [
        "'Experiment Data': merged cells changed",
        "'Experiment Data'!B3: value 'Synth' -> 'Other'",
    ]


def test_lost_file_parts_are_reported(synthetic_xlsx):
    before = with_part(synthetic_xlsx, 'xl/media/image1.png')
    assert workbook_problems(before, synthetic_xlsx) == [
        'file part xl/media/image1.png lost'
    ]


def test_parts_without_lab_data_are_ignored(synthetic_xlsx):
    before = synthetic_xlsx
    for part in ('xl/calcChain.xml', 'xl/printerSettings/printerSettings1.bin'):
        before = with_part(before, part)
    assert workbook_problems(before, synthetic_xlsx) == []
