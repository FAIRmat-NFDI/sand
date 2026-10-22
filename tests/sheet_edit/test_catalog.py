import datetime

import pytest
from synthetic_sheet import edited, letter, with_formula_results

from sand.sheet_edit.catalog import (
    Catalog,
    ColumnInfo,
    Family,
    build,
    sample_ranges,
    value_type,
)

INFO = 'Experiment Info'
CLEANING = '1: Cleaning UV-Ozone'
SPIN2 = '2: Spin Coating'
SEQ = '4: Seq-Evaporation'
CSS = '5: Close Space Sublimation'
REAL_KIT_COLUMNS = 155


def entry(catalog: Catalog, section: str, header: str) -> ColumnInfo:
    return next(
        c
        for c in catalog.columns
        if (c.column.section.label, c.column.header) == (section, header)
    )


def test_one_entry_per_column_with_ids_from_the_column_place(synthetic_xlsx):
    catalog = build(synthetic_xlsx)
    assert len(catalog.columns) == len(catalog.structure.columns)
    assert entry(catalog, INFO, 'Date').id == 'C001'
    assert all(c.id == f'C{c.column.index:03d}' for c in catalog.columns)


def test_ids_are_stable_after_a_value_change(synthetic_xlsx):
    speed = letter(SPIN2, 'Rotation speed [rpm]')

    def change(ws):
        ws[f'{speed}3'] = 4000

    before, after = build(synthetic_xlsx), build(edited(synthetic_xlsx, change))
    assert [(c.id, c.column.header) for c in before.columns] == [
        (c.id, c.column.header) for c in after.columns
    ]


@pytest.mark.parametrize(
    ('section', 'header', 'family'),
    [
        (CLEANING, 'Solvent 2', Family('cleaning_steps', 2)),
        (CLEANING, 'Time 2 [s]', Family('cleaning_steps', 2)),
        (CLEANING, 'UV-Ozone Time [s]', None),
        (SPIN2, 'Solvent 1 name', Family('solvents', 1)),
        (SPIN2, 'Solvent 1 volume [uL]', Family('solvents', 1)),
        (SPIN2, 'Solute 1 type', Family('solutes', 1)),
        (SPIN2, 'Solute 1 Concentration [mM]', Family('solutes', 1)),
        (SPIN2, 'Rotation speed [rpm]', Family('rotation_steps', 1)),
        (SPIN2, 'Solution volume [uL]', None),
        (SEQ, 'Material name', None),
        (SEQ, 'Material name 2', Family(None, 2)),
        (SEQ, 'Thickness 2 [nm]', Family(None, 2)),
        (SEQ, 'Rate 1 [angstrom/s]', Family(None, 1)),
        (CSS, 'Source material name 1', Family(None, 1)),
    ],
)
def test_families_of_numbered_columns(synthetic_xlsx, section, header, family):
    assert entry(build(synthetic_xlsx), section, header).family == family


@pytest.mark.parametrize(
    ('section', 'header', 'unit'),
    [
        (SPIN2, 'Rotation speed [rpm]', 'rpm'),
        (INFO, 'Sample area [cm^2]', 'cm^2'),
        (SEQ, 'Rate 2 [angstrom/s]', 'angstrom/s'),
        (SPIN2, 'Material name', None),
    ],
)
def test_unit_comes_from_the_header(synthetic_xlsx, section, header, unit):
    assert entry(build(synthetic_xlsx), section, header).unit == unit


@pytest.mark.parametrize(
    ('section', 'header', 'value_type'),
    [
        (INFO, 'Date', 'yyyymmdd'),
        (INFO, 'Batch', 'text'),
        (INFO, 'Sample', 'int'),
        (INFO, 'Sample area [cm^2]', 'float'),
        (SPIN2, 'Rotation speed [rpm]', 'int'),
        (SPIN2, 'Notes', 'empty'),
    ],
)
def test_type_comes_from_the_cells(synthetic_xlsx, section, header, value_type):
    assert entry(build(synthetic_xlsx), section, header).type == value_type


@pytest.mark.parametrize(
    ('header', 'values', 'expected'),
    [
        ('Rate', [1, 2.5, None], 'float'),
        ('Rate', [1, 'n/a'], 'mixed'),
        ('Rate', [None, '  '], 'empty'),
        ('Flag', [True, False], 'bool'),
        ('Datetime', [datetime.datetime(2026, 9, 14, 10, 0)], 'date'),
        ('Date', [20260914, 20260915.0, None], 'yyyymmdd'),
        ('Date', [20261399], 'int'),  # not a calendar day
        ('Date', [2026], 'int'),
        ('Batch', [20260914], 'int'),  # the header does not say date
    ],
)
def test_value_type(header, values, expected):
    assert value_type(header, values) == expected


def test_formula_columns_are_marked_with_the_columns_they_read(synthetic_xlsx):
    catalog = build(synthetic_xlsx)
    deposition = entry(catalog, CSS, 'Deposition Time [s]')
    nomad_id = entry(catalog, INFO, 'Nomad ID')
    assert deposition.formula
    assert deposition.inputs == (entry(catalog, INFO, 'Variation').id,)
    assert nomad_id.formula
    assert nomad_id.inputs == tuple(
        entry(catalog, INFO, h).id
        for h in ('Date', 'Project_Name', 'Batch', 'Subbatch', 'Sample')
    )
    assert [c.column.header for c in catalog.columns if c.formula] == [
        'Nomad ID',
        'Deposition Time [s]',
    ]
    assert entry(catalog, INFO, 'Variation').inputs == ()


@pytest.mark.parametrize(
    'formula',
    [
        '=SUM(A3:B3)',  # a function the calculator does not know
        '=ZZ3*2',  # a column without a header
    ],
)
def test_formula_inputs_are_unknown_rather_than_guessed(synthetic_xlsx, formula):
    deposition = letter(CSS, 'Deposition Time [s]')

    def change(ws):
        ws[f'{deposition}4'] = formula

    entry_ = entry(build(edited(synthetic_xlsx, change)), CSS, 'Deposition Time [s]')
    assert entry_.formula
    assert entry_.inputs is None


def test_real_kit_sheet(real_kit_xlsx):
    catalog = build(real_kit_xlsx)
    assert len(catalog.columns) == REAL_KIT_COLUMNS
    assert [c.column.header for c in catalog.columns if c.formula] == [
        'Nomad ID',
        'Deposition Time [s]',
    ]
    assert entry(catalog, CSS, 'Deposition Time [s]').inputs == (
        entry(catalog, INFO, 'Variation').id,
    )
    assert entry(catalog, INFO, 'Date').type == 'yyyymmdd'
    assert entry(catalog, CSS, 'Source material ratio 2').family == Family(None, 2)


# --- samples and value groups ---------------------------------------------


def groups(catalog: Catalog, section: str, header: str) -> list[tuple]:
    label = {s.row: s.label for s in catalog.samples}
    return [
        (g.value, sample_ranges([label[r] for r in g.rows]))
        for g in entry(catalog, section, header).groups
    ]


def test_samples_are_named_by_their_label_and_keep_the_gap(synthetic_xlsx):
    catalog = build(synthetic_xlsx)
    assert [(s.row, s.label) for s in catalog.samples][4:6] == [(7, '5'), (8, '7')]
    assert sample_ranges([s.label for s in catalog.samples]) == '1–5, 7–13'
    assert {s.subbatch for s in catalog.samples} == {'0'}
    assert not catalog.needs_subbatch


def test_nomad_id_is_the_result_excel_stored(synthetic_xlsx):
    nomad_id = letter(INFO, 'Nomad ID')
    xlsx = with_formula_results(synthetic_xlsx, {f'{nomad_id}3': 'KIT_Synth_1'})
    assert [s.nomad_id for s in build(xlsx).samples][:2] == ['KIT_Synth_1', None]


def test_subbatches_restarting_at_1_need_the_subbatch(synthetic_xlsx):
    subbatch, sample = letter(INFO, 'Subbatch'), letter(INFO, 'Sample')

    def second_subbatch(ws):
        for i, r in enumerate(range(9, 15)):
            ws[f'{subbatch}{r}'] = 1
            ws[f'{sample}{r}'] = i + 1

    catalog = build(edited(synthetic_xlsx, second_subbatch))
    assert catalog.needs_subbatch
    assert [(s.subbatch, s.label) for s in catalog.samples][5:7] == [
        ('0', '7'),
        ('1', '1'),
    ]


def test_rows_without_a_label_are_not_a_subbatch_restart(synthetic_xlsx):
    sample = letter(INFO, 'Sample')

    def unlabel(ws):
        ws[f'{sample}3'] = None
        ws[f'{sample}4'] = None

    assert not build(edited(synthetic_xlsx, unlabel)).needs_subbatch


def test_value_groups(synthetic_xlsx):
    catalog = build(synthetic_xlsx)
    assert groups(catalog, CSS, 'Substrate temperature [°C]') == [
        (120, '1–5, 7'),
        (115, '8–13'),
    ]
    assert groups(catalog, INFO, 'Variation') == [
        ('10min', '1–3'),
        ('20min', '4–5, 7'),
        ('10min-hot', '8–10'),
        ('20min-hot', '11–13'),
    ]
    assert groups(catalog, SPIN2, 'Rotation speed [rpm]') == [(3000, '1–5, 7–13')]
    assert groups(catalog, SPIN2, 'Notes') == [(None, '1–5, 7–13')]


@pytest.mark.parametrize(('value', 'expected'), [('3000', '3000'), (True, True)])
def test_text_and_bool_are_not_the_same_value_as_a_number(
    synthetic_xlsx, value, expected
):
    speed = letter(SPIN2, 'Rotation speed [rpm]')

    def change(ws):
        ws[f'{speed}3'] = value

    catalog = build(edited(synthetic_xlsx, change))
    assert groups(catalog, SPIN2, 'Rotation speed [rpm]') == [
        (expected, '1'),
        (3000, '2–5, 7–13'),
    ]


@pytest.mark.parametrize(
    ('labels', 'expected'),
    [
        (['1', '2', '3', '7', '9', '10', '11', '12'], '1–3, 7, 9–12'),
        (['1', '2', '4'], '1–2, 4'),
        (['3', '2', '1'], '3, 2, 1'),  # in sheet order, never sorted
        (['A', 'B', '1'], 'A, B, 1'),
        (['07', '08'], '07, 08'),  # kept as written
        (['s_1', 's_2', 's_3', 's_5'], 's_1–s_3, s_5'),
        (['s_1', 't_2'], 's_1, t_2'),
        (['1.4', '1.5'], '1.4, 1.5'),
        ([], ''),
    ],
)
def test_sample_ranges(labels, expected):
    assert sample_ranges(labels) == expected


def test_real_kit_samples_and_groups(real_kit_xlsx):
    catalog = build(real_kit_xlsx)
    assert sample_ranges([s.label for s in catalog.samples]) == '1–21, 23–43'
    assert groups(catalog, CSS, 'Substrate temperature [°C]') == [
        (120, '1–21'),
        (115, '23–43'),
    ]
    assert all(
        s.nomad_id and s.nomad_id.endswith(f'_{s.label}') for s in catalog.samples
    )
