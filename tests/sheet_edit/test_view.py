"""The sheet view: fields, steps, variations, warnings and changed cells
read from the synthetic sheet (and the real one when it is there)."""

import pytest
from synthetic_sheet import SAMPLE_LABELS, edited, letter
from test_changelog import SOLVENT, SPEED_4000, VARIATION_SAID, logged

from sand.sheet_edit.catalog import build
from sand.sheet_edit.view import sheet_view

STEPS = 8
TEMPERATURE = 'Substrate temperature [°C]'
CSS = '5: Close Space Sublimation'


def step(view: dict, position: int) -> dict:
    return next(s for s in view['steps'] if s['position'] == position)


def param(step: dict, header: str) -> dict:
    return next(p for p in step['params'] if p['header'] == header)


@pytest.fixture(scope='module')
def view(synthetic_xlsx) -> dict:
    return sheet_view(build(synthetic_xlsx))


def test_info_is_what_holds_for_every_sample(view):
    assert view['sheet'] == 'Experiment Data'
    assert view['info']['samples'] == len(SAMPLE_LABELS)
    assert view['info']['subbatches'] == ['0']
    assert view['info']['rows'][:2] == [
        {'row': 3, 'label': '1', 'subbatch': '0'},
        {'row': 4, 'label': '2', 'subbatch': '0'},
    ]
    assert [r['label'] for r in view['info']['rows']] == [str(n) for n in SAMPLE_LABELS]
    fields = {f['header']: f for f in view['info']['fields']}
    # the identity columns, the per-sample Variation and the empty Notes are not fields
    assert list(fields) == ['Date', 'Project_Name', 'Batch', 'Sample area [cm^2]']
    assert fields['Date'] == {
        'column_id': 'C001',
        'header': 'Date',
        'name': 'Date',
        'unit': None,
        'type': 'yyyymmdd',
        'value': '20250101',
    }
    assert (
        fields['Sample area [cm^2]']['name'],
        fields['Sample area [cm^2]']['unit'],
    ) == (
        'Sample area',
        'cm^2',
    )
    assert fields['Sample area [cm^2]']['value'] == '0.1'


def test_steps_in_sheet_order_with_their_filled_parameters(view):
    assert [s['position'] for s in view['steps']] == list(range(1, STEPS + 1))
    spin = step(view, 2)
    assert (spin['step_type'], spin['label']) == ('Spin Coating', '2: Spin Coating')
    assert (spin['material'], spin['layer']) == ('Me-4PACz', 'HTL')
    assert not spin['empty'] and not spin['varies']
    # an empty column is no parameter
    assert 'Solvent 1 volume [uL]' not in {p['header'] for p in spin['params']}
    assert param(spin, 'Rotation speed [rpm]') == {
        'column_id': 'C025',
        'header': 'Rotation speed [rpm]',
        'name': 'Rotation speed',
        'unit': 'rpm',
        'type': 'int',
        'family': {'group': 'rotation_steps', 'index': 1},
        'formula': False,
        'values': [
            {'value': '3000', 'rows': list(range(3, 15)), 'samples': 'all 12 samples'}
        ],
    }
    cleaning = step(view, 1)
    assert (cleaning['material'], cleaning['layer']) == (None, None)
    assert [p['name'] for p in cleaning['params']][:3] == [
        'Solvent 1',
        'Time 1',
        'Temperature 1',
    ]


def test_an_empty_step_is_marked_and_warned(view):
    empty = step(view, 7)
    assert empty['empty'] and empty['params'] == []
    assert (empty['material'], empty['layer']) == (None, None)
    assert 'Step 7 (Spin Coating) is empty' in view['warnings']


def test_a_step_varies_when_a_parameter_does(view):
    css = step(view, 5)
    assert css['varies']
    assert param(css, TEMPERATURE)['values'] == [
        {'value': '120', 'rows': [3, 4, 5, 6, 7, 8], 'samples': 'samples 1–5, 7'},
        {'value': '115', 'rows': [9, 10, 11, 12, 13, 14], 'samples': 'samples 8–13'},
    ]
    # the formula column has no stored results in the synthetic file: empty, no param
    assert 'Deposition Time [s]' not in {p['header'] for p in css['params']}


def test_variations_are_the_columns_with_more_than_one_value(view):
    assert [(v['step'], v['header']) for v in view['variations']] == [
        ('Experiment Info', 'Variation'),
        (CSS, TEMPERATURE),
    ]
    variation = view['variations'][0]
    assert variation['column_id'] == 'C007'
    assert [(g['value'], g['samples']) for g in variation['values']] == [
        ('10min', 'samples 1–3'),
        ('20min', 'samples 4–5, 7'),
        ('10min-hot', 'samples 8–10'),
        ('20min-hot', 'samples 11–13'),
    ]


def test_a_gap_in_the_sample_numbers_is_warned(view):
    assert view['warnings'] == ['Sample 6 is missing', 'Step 7 (Spin Coating) is empty']


def test_several_gaps_and_a_label_used_twice(synthetic_xlsx):
    sample = letter('Experiment Info', 'Sample')

    def change(ws):
        ws[f'{sample}4'] = 1  # sample 2 becomes a second sample 1
        ws[f'{sample}5'] = 30  # sample 3 becomes 30: 8 to 29 are missing too

    view = sheet_view(build(edited(synthetic_xlsx, change)))
    # a label used twice makes the subbatch part of every sample's name
    assert view['warnings'][:2] == [
        'Samples 2–3, 6, 14–29 of subbatch 0 are missing',
        'Sample 1 of subbatch 0 appears twice',
    ]


def test_the_subbatch_is_named_when_labels_repeat(synthetic_xlsx):
    sample = letter('Experiment Info', 'Sample')
    subbatch = letter('Experiment Info', 'Subbatch')

    def change(ws):
        # rows 9 to 14 become subbatch 1, samples 1, 2, 4, 5, 7, 8
        for r, n in zip(range(9, 15), (1, 2, 4, 5, 7, 8)):
            ws[f'{subbatch}{r}'] = 1
            ws[f'{sample}{r}'] = n

    view = sheet_view(build(edited(synthetic_xlsx, change)))
    assert view['info']['subbatches'] == ['0', '1']
    assert view['warnings'][:2] == [
        'Sample 6 of subbatch 0 is missing',
        'Samples 3, 6 of subbatch 1 are missing',
    ]


def test_a_row_without_a_sample_number(synthetic_xlsx):
    sample = letter('Experiment Info', 'Sample')
    view = sheet_view(
        build(edited(synthetic_xlsx, lambda ws: ws.__setitem__(f'{sample}4', None)))
    )
    assert view['warnings'][0] == 'Row 4 has no Sample value'


def test_values_that_differ_only_by_spaces_are_one(synthetic_xlsx):
    notes = letter(CSS, 'Notes')

    def change(ws):
        for r in range(3, 15):
            ws[f'{notes}{r}'] = '8 times'
        ws[f'{notes}3'] = '8 times '

    view = sheet_view(build(edited(synthetic_xlsx, change)))
    assert param(step(view, 5), 'Notes')['values'] == [
        {'value': '8 times', 'rows': list(range(3, 15)), 'samples': 'all 12 samples'}
    ]
    assert [v['header'] for v in view['variations']] == ['Variation', TEMPERATURE]


def test_a_number_typed_as_text_is_one_value_but_warned(synthetic_xlsx):
    speed = letter('2: Spin Coating', 'Rotation speed [rpm]')
    view = sheet_view(
        build(edited(synthetic_xlsx, lambda ws: ws.__setitem__(f'{speed}3', '3000')))
    )
    rpm = param(step(view, 2), 'Rotation speed [rpm]')
    assert (rpm['type'], [v['value'] for v in rpm['values']]) == ('mixed', ['3000'])
    assert (
        'Rotation speed [rpm] in 2: Spin Coating mixes numbers and text'
        in view['warnings']
    )


def test_a_name_that_counts_up_row_by_row_is_warned(synthetic_xlsx):
    """PbBr2, PbBr3, ... down a name column is Excel's drag-fill, not
    fourteen solutes."""
    material = letter('2: Spin Coating', 'Material name')

    def drag_fill(ws):
        for i, row in enumerate(range(3, 15)):
            ws[f'{material}{row}'] = f'PbBr{i + 2}'

    view = sheet_view(build(edited(synthetic_xlsx, drag_fill)))
    assert (
        'Material name in 2: Spin Coating counts up from PbBr2 to PbBr13: '
        'an Excel drag-fill?'
    ) in view['warnings']
    # numbers that do not grow by one, or several prefixes, are no drag-fill
    view = sheet_view(
        build(
            edited(
                synthetic_xlsx,
                lambda ws: [
                    ws.__setitem__(f'{material}3', 'PbBr2'),
                    ws.__setitem__(f'{material}4', 'PbBr5'),
                    ws.__setitem__(f'{material}5', 'PbBr6'),
                ],
            )
        )
    )
    assert not [w for w in view['warnings'] if 'counts up' in w]


def test_a_varying_material_is_no_headline(synthetic_xlsx):
    material = letter('2: Spin Coating', 'Material name')
    view = sheet_view(
        build(edited(synthetic_xlsx, lambda ws: ws.__setitem__(f'{material}3', 'PTAA')))
    )
    spin = step(view, 2)
    assert (spin['material'], spin['layer'], spin['varies']) == (None, 'HTL', True)


def test_prefixed_sample_labels(synthetic_xlsx):
    sample = letter('Experiment Info', 'Sample')

    def prefixed(ws):
        for r in range(3, 15):
            ws[f'{sample}{r}'] = f's_{ws[f"{sample}{r}"].value}'

    view = sheet_view(build(edited(synthetic_xlsx, prefixed)))
    assert view['warnings'][0] == 'Sample s_6 is missing'
    assert view['variations'][0]['values'][0]['samples'] == 'samples s_1–s_3'


def test_labels_that_are_not_numbers_have_no_gaps(synthetic_xlsx):
    sample = letter('Experiment Info', 'Sample')

    def lettered(ws):
        for r in range(3, 15):
            ws[f'{sample}{r}'] = 'ABCDEFGHIJKL'[r - 3]

    view = sheet_view(build(edited(synthetic_xlsx, lettered)))
    assert view['warnings'] == ['Step 7 (Spin Coating) is empty']


def test_a_parameter_empty_on_some_samples_varies(synthetic_xlsx):
    speed = letter('2: Spin Coating', 'Rotation speed [rpm]')
    view = sheet_view(
        build(edited(synthetic_xlsx, lambda ws: ws.__setitem__(f'{speed}3', None)))
    )
    spin = step(view, 2)
    assert spin['varies']
    assert param(spin, 'Rotation speed [rpm]')['values'] == [
        {'value': None, 'rows': [3], 'samples': 'sample 1'},
        {'value': '3000', 'rows': list(range(4, 15)), 'samples': 'samples 2–5, 7–13'},
    ]


# --- changed cells -----------------------------------------------------------


def test_changed_is_per_cell_old_against_new(synthetic_xlsx):
    log, final = logged(synthetic_xlsx, [SPEED_4000, SOLVENT])
    view = sheet_view(build(final), log)
    assert param(step(view, 2), 'Rotation speed [rpm]')['values'][0]['value'] == '4000'
    changed = view['changed']
    # in sheet order: the solvent column (M) before the speed (Y)
    assert [c['header'] for c in changed[:3]] == ['Solvent 2'] * 3
    assert changed[0] == {
        'column_id': 'C013',
        'step': '1: Cleaning UV-Ozone',
        'header': 'Solvent 2',
        'row': 3,
        'sample': '1',
        'old': 'IPA',
        'new': 'DMSO',
        'by_formula': False,
    }
    assert [(c['row'], c['old'], c['new']) for c in changed[3:]] == [
        (r, '3000', '4000') for r in range(3, 15)
    ]


def test_a_recomputed_formula_result_is_a_change_by_formula(with_results):
    log, final = logged(with_results, [VARIATION_SAID])
    view = sheet_view(build(final), log)
    variation, deposition = view['changed']
    assert (variation['header'], variation['old'], variation['new']) == (
        'Variation',
        '10min',
        '30min',
    )
    assert not variation['by_formula']
    assert (deposition['header'], deposition['old'], deposition['new']) == (
        'Deposition Time [s]',
        '600',
        '1800',
    )
    assert deposition['by_formula']


def test_a_changed_cell_is_named_as_the_sheet_names_its_row_now(synthetic_xlsx):
    sample = letter('Experiment Info', 'Sample')
    subbatch = letter('Experiment Info', 'Subbatch')
    log, final = logged(synthetic_xlsx, [SOLVENT])
    # the row lost its Sample value since: the log said '1', the sheet says 'row 3'
    view = sheet_view(
        build(edited(final, lambda ws: ws.__setitem__(f'{sample}3', None))), log
    )
    assert [c['sample'] for c in view['changed']] == ['row 3', '2', '3']

    # labels repeat since (row 4 became sample 1 of subbatch 1): the
    # subbatch is part of every name
    def repeat(ws):
        ws[f'{sample}4'] = 1
        ws[f'{subbatch}4'] = 1

    view = sheet_view(build(edited(final, repeat)), log)
    assert [c['sample'] for c in view['changed']] == [
        '1 of subbatch 0',
        '1 of subbatch 1',
        '3 of subbatch 0',
    ]

    # row 5 lost both its Sample and its Subbatch: 'row 5', no subbatch
    def repeat_and_clear(ws):
        repeat(ws)
        ws[f'{sample}5'] = None
        ws[f'{subbatch}5'] = None

    view = sheet_view(build(edited(final, repeat_and_clear)), log)
    assert [c['sample'] for c in view['changed']] == [
        '1 of subbatch 0',
        '1 of subbatch 1',
        'row 5',
    ]


def test_without_a_log_nothing_is_changed(view):
    assert view['changed'] == []


# --- the real sheet ----------------------------------------------------------


def test_the_real_sheet(real_kit_xlsx):
    view = sheet_view(build(real_kit_xlsx))
    assert view['warnings'] == [
        'Sample 22 is missing',
        'Step 7 (Spin Coating) is empty',
    ]
    assert [f['header'] for f in view['info']['fields']] == [
        'Date',
        'Project_Name',
        'Batch',
        'Sample dimension',
        'Sample area [cm^2]',
        'Number of pixels',
        'Pixel area',
        'Number of junctions',
        'Substrate material',
        'Substrate conductive layer',
    ]
    assert [s['varies'] for s in view['steps']].count(True) == 1
    css = step(view, 5)
    assert (css['material'], css['layer']) == ('MAI+MABr', 'Organic')
    # the deposition time follows the variation by formula: it varies too
    assert [(v['header'], len(v['values'])) for v in view['variations']] == [
        ('Variation', 14),
        (TEMPERATURE, 2),
        ('Deposition Time [s]', 7),
    ]
