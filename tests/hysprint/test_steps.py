import json

import pytest

from sand.hysprint.steps import (
    fill_schema,
    normalize_variants,
    select_schema,
    step_types,
)

MIN_STEP_TYPES = 10  # the schema defines ~15; guard against a truncated artifact


def test_step_types_come_from_the_schema_artifact():
    types = step_types()
    assert 'Spin Coating' in types
    assert len(types) >= MIN_STEP_TYPES


def test_select_schema_enumerates_all_step_types():
    schema = select_schema()
    assert schema['properties']['step_type']['enum'] == list(step_types())
    assert schema['required'] == ['step_type']


def test_fill_schema_slices_one_step_type():
    schema = fill_schema('spin coating')  # casing normalized via $defs key
    assert schema['properties']['step_type'] == {'const': 'Spin Coating'}
    items = schema['properties']['variants']['items']
    assert 'samples' in items['properties']
    # bookkeeping is addressing/identity, never step content
    for bookkeeping in ('position_in_experimental_plan', 'datetime', 'operator'):
        assert bookkeeping not in items['properties']
    # NOMAD unit keys are not valid JSON Schema
    assert '"unit"' not in json.dumps(schema)


def test_fill_schema_rejects_unknown_step_type():
    with pytest.raises(ValueError, match='unknown step type'):
        fill_schema('Underwater Welding')


def test_normalize_variants_bares_sample_labels():
    slot = {
        'step_type': 'Spin Coating',
        'variants': [
            {'samples': ['sample 1', 'Samples 2', '3']},
            {'samples': 'all'},
        ],
    }
    normalize_variants(slot)
    assert slot['variants'][0]['samples'] == ['1', '2', '3']
    assert slot['variants'][1]['samples'] == 'all'
