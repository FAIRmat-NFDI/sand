import io

from kit_like import SECTIONS
from openpyxl import Workbook

from sand.sheet_edit.workbook import read_structure

REAL_KIT_COLUMNS = 155
REAL_KIT_SAMPLES = 42


def _xlsx(rows: list[list]) -> bytes:
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_sections_keep_order_position_and_repeated_step_types(kit_like_xlsx):
    s = read_structure(kit_like_xlsx)
    assert [(sec.position, sec.step_type) for sec in s.sections] == [
        (None, 'Experiment Info'),
        (1, 'Cleaning UV-Ozone'),
        (2, 'Spin Coating'),
        (3, 'Spin Coating'),
        (4, 'Seq-Evaporation'),
        (5, 'Close Space Sublimation'),
        (6, 'Annealing'),
        (7, 'Spin Coating'),
        (8, 'Evaporation'),
    ]
    assert s.sheet_name == 'Experiment Data'


def test_columns_belong_to_their_section(kit_like_xlsx):
    s = read_structure(kit_like_xlsx)
    spin2, spin3 = s.sections[2], s.sections[3]
    speeds = [c for c in s.columns if c.header == 'Rotation speed [rpm]']
    # same header in three spin coatings: told apart only by section
    assert [c.section.position for c in speeds] == [2, 3, 7]
    assert speeds[0].section == spin2 and speeds[1].section == spin3
    assert all(c.section.first_col <= c.index <= c.section.last_col for c in s.columns)
    assert s.columns[0].index == 1 and s.columns[0].header == 'Date'
    assert len(s.columns) == sum(len(headers) for _, headers in SECTIONS)


def test_sample_rows_carry_sample_names_and_keep_the_gap(kit_like_xlsx):
    s = read_structure(kit_like_xlsx)
    assert [r.row for r in s.samples] == list(range(3, 15))
    assert [r.sample for r in s.samples] == [
        '1',
        '2',
        '3',
        '4',
        '5',
        '7',
        '8',
        '9',
        '10',
        '11',
        '12',
        '13',
    ]


def test_unmerged_group_label_spans_until_the_next_label():
    # the batch parser forward-fills row 1, so merging is not required
    s = read_structure(
        _xlsx(
            [
                ['Experiment Info', None, '1: Annealing', None],
                ['Sample', 'Nomad ID', 'Annealing time [min]', 'Notes'],
                [1, 'KIT_x_1', 10, None],
            ]
        )
    )
    assert [(sec.label, sec.first_col, sec.last_col) for sec in s.sections] == [
        ('Experiment Info', 1, 2),
        ('1: Annealing', 3, 4),
    ]


def test_blank_trailing_rows_are_not_samples_and_float_sample_names_are_bare():
    s = read_structure(
        _xlsx(
            [
                ['Experiment Info', None],
                ['Sample', 'Nomad ID'],
                [1.0, 'a'],
                [2, 'b'],
                [None, None],
            ]
        )
    )
    assert [(r.row, r.sample) for r in s.samples] == [(3, '1'), (4, '2')]


def test_no_sample_column_gives_rows_without_sample_names():
    s = read_structure(_xlsx([['Experiment Info'], ['Nomad ID'], ['KIT_a']]))
    assert [(r.row, r.sample) for r in s.samples] == [(3, None)]


def test_real_kit_sheet(real_kit_xlsx):
    s = read_structure(real_kit_xlsx)
    assert [sec.position for sec in s.sections] == [None, *range(1, 10)]
    assert [sec.position for sec in s.sections if sec.step_type == 'Spin Coating'] == [
        2,
        3,
        7,
    ]
    assert len(s.columns) == REAL_KIT_COLUMNS
    names = [r.sample for r in s.samples]
    assert len(names) == REAL_KIT_SAMPLES and '22' not in names
    assert names[0] == '1' and names[-1] == '43'
