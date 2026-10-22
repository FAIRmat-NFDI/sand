import json

import pytest
from pydantic import ValidationError
from synthetic_sheet import edited, letter

from sand.sheet_edit.catalog import build
from sand.sheet_edit.edits import Edit, answer_model, answer_schema

INFO = 'Experiment Info'
REAL_KIT_WRITABLE = 153  # 155 columns minus Nomad ID and Deposition Time


@pytest.fixture(scope='module')
def catalog(synthetic_xlsx):
    return build(synthetic_xlsx)


@pytest.fixture(scope='module')
def ids(catalog):
    """Column ids by header (first match in sheet order)."""
    found: dict[str, str] = {}
    for c in catalog.columns:
        found.setdefault(c.column.header, c.id)
    return found


def answer(edits=(), unresolved=()) -> dict:
    return {'edits': list(edits), 'unresolved': list(unresolved)}


def edit(target, rows='all', anchor=None, old='3000', new='4000') -> dict:
    return {
        'target': target,
        'rows': rows,
        'anchor': anchor,
        'old': old,
        'new': new,
        'said': 'speed 4000',
    }


@pytest.mark.parametrize(
    'rows',
    [
        'all',
        {'samples': ['1', '2', '3'], 'subbatch': None},
        {'first': '1', 'last': '13', 'subbatch': '0'},
        {'where': 'C007', 'equals': '10min'},
        {'where': 'C056', 'equals': '600'},  # a formula column may select rows
        {'where': 'C009', 'equals': None},  # the rows where Notes is empty
    ],
)
def test_every_row_form_is_accepted(catalog, ids, rows):
    model = answer_model(catalog)
    result = model.model_validate(answer([edit(ids['Rotation speed [rpm]'], rows)]))
    assert isinstance(result.edits[0], Edit)


def test_an_anchor_and_an_unresolved_are_accepted(catalog, ids):
    model = answer_model(catalog)
    model.model_validate(
        answer(
            [
                edit(
                    ids['Solvent 1 volume [uL]'],
                    anchor={'column': ids['Solvent 1 name'], 'equals': 'Ethanol'},
                    old=None,
                    new='60',
                )
            ],
            [
                {
                    'reason': 'ambiguous',
                    'candidates': ['C028', 'C041', 'C059'],
                    'said': 'annealing temperature 140',
                }
            ],
        )
    )


@pytest.mark.parametrize(
    ('bad', 'why'),
    [
        (edit('C999'), 'unknown column'),
        (edit('C056'), 'formula column (Deposition Time)'),
        (edit('C006'), 'formula column (Nomad ID)'),
        (edit('C025', {'samples': ['6'], 'subbatch': None}), 'no sample 6'),
        (edit('C025', {'samples': [], 'subbatch': None}), 'no samples'),
        (edit('C025', {'first': '1', 'last': '22', 'subbatch': None}), 'no 22'),
        (edit('C025', {'samples': ['1'], 'subbatch': '1'}), 'no subbatch 1'),
        (edit('C025', {'where': 'C999', 'equals': 'x'}), 'unknown where column'),
        (edit('C025', anchor={'column': 'C999', 'equals': 'x'}), 'unknown anchor'),
        (edit('C025', 'some'), 'not a row form'),
        ({**edit('C025'), 'unit': 'rpm'}, 'an extra field'),
        ({k: v for k, v in edit('C025').items() if k != 'anchor'}, 'anchor missing'),
    ],
)
def test_invalid_proposals_are_rejected(catalog, bad, why):
    with pytest.raises(ValidationError):
        answer_model(catalog).model_validate(answer([bad]))


def test_unresolved_candidates_must_exist(catalog):
    with pytest.raises(ValidationError):
        answer_model(catalog).model_validate(
            answer(
                unresolved=[
                    {'reason': 'ambiguous', 'candidates': ['C999'], 'said': 'x'}
                ]
            )
        )


def test_schema_enums_are_the_sheet_ids_and_labels(catalog):
    schema = answer_schema(catalog)
    json.dumps(schema)  # serializable as it is sent
    defs = schema['$defs']
    target = defs['Edit']['properties']['target']['enum']
    assert 'C056' not in target and 'C006' not in target
    assert len(target) == len(catalog.columns) - 2
    assert defs['Samples']['properties']['samples']['items']['enum'] == [
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
    assert all(d.get('additionalProperties') is False for d in defs.values())


def test_subbatches_are_offered_when_the_sheet_has_them(synthetic_xlsx):
    subbatch, sample = letter(INFO, 'Subbatch'), letter(INFO, 'Sample')

    def second_subbatch(ws):
        for i, r in enumerate(range(9, 15)):
            ws[f'{subbatch}{r}'] = 1
            ws[f'{sample}{r}'] = i + 1

    model = answer_model(build(edited(synthetic_xlsx, second_subbatch)))
    rows = {'samples': ['1'], 'subbatch': '1'}
    model.model_validate(answer([edit('C025', rows)]))


def test_a_sheet_without_samples_cannot_be_edited(synthetic_xlsx):
    headers_only = edited(synthetic_xlsx, lambda ws: ws.delete_rows(3, 100))
    with pytest.raises(ValueError, match='no sample labels'):
        answer_model(build(headers_only))


def test_real_kit_sheet(real_kit_xlsx):
    schema = answer_schema(build(real_kit_xlsx))
    target = schema['$defs']['Edit']['properties']['target']['enum']
    assert len(target) == REAL_KIT_WRITABLE
    assert (
        '22' not in schema['$defs']['Samples']['properties']['samples']['items']['enum']
    )
