import json

import pytest
from nomad.utils import generate_entry_id

from sand.hysprint import EXPERIMENT_INFO_MAINFILE
from sand.hysprint.generate import (
    HysprintInputError,
    assemble,
    resolve_sample_labels,
    route_inputs,
)
from sand.services.voice_eln import CollectedInput

UPLOAD_ID = 'up-1'
INFO_ENTRY_ID = generate_entry_id(UPLOAD_ID, EXPERIMENT_INFO_MAINFILE)

INFO = {
    'project_name': 'perov',
    'batch': 'B1',
    'subbatch': 'a',
    'first_sample': '1',
    'n_samples': 2,
}


def _input(entry_id, text, label='', kind='note'):
    return CollectedInput(
        entry_id=entry_id, kind=kind, text=text, label=label, datetime=None
    )


def _info_input(info=INFO):
    return _input(INFO_ENTRY_ID, json.dumps(info), label='experiment_info')


def test_route_inputs_separates_info_from_ordered_steps():
    inputs = [
        _info_input(),
        _input('a-1', 'cleaned the substrates', kind='audio'),
        _input('n-1', 'spin coated NiOx'),
    ]

    info, steps = route_inputs(inputs, UPLOAD_ID)

    assert info == INFO
    assert steps == ['cleaned the substrates', 'spin coated NiOx']


def test_route_inputs_ignores_user_labels():
    # the form is found by its mainfile, so labels can be anything
    inputs = [
        _input(INFO_ENTRY_ID, json.dumps(INFO), label='my form'),
        _input('n-1', 'spin coated NiOx', label='experiment_info'),
    ]

    info, steps = route_inputs(inputs, UPLOAD_ID)

    assert info == INFO
    assert steps == ['spin coated NiOx']


def test_route_inputs_requires_experiment_info():
    with pytest.raises(HysprintInputError, match='experiment_info'):
        route_inputs([_input('n-1', 'a step')], UPLOAD_ID)


def test_route_inputs_rejects_invalid_info_json():
    inputs = [_input(INFO_ENTRY_ID, 'not json')]
    with pytest.raises(HysprintInputError, match='not valid JSON'):
        route_inputs(inputs, UPLOAD_ID)


def test_route_inputs_rejects_incomplete_info():
    inputs = [
        _info_input({'project_name': 'perov'}),
        _input('n-1', 'a step'),
    ]
    with pytest.raises(HysprintInputError, match='missing fields'):
        route_inputs(inputs, UPLOAD_ID)


def test_route_inputs_rejects_textless_input():
    inputs = [_info_input(), _input('a-1', None, kind='audio')]
    with pytest.raises(HysprintInputError, match='no text yet'):
        route_inputs(inputs, UPLOAD_ID)


def test_route_inputs_allows_experiment_without_steps():
    info, steps = route_inputs([_info_input()], UPLOAD_ID)
    assert info['project_name'] == 'perov'
    assert steps == []


def test_route_inputs_coerces_n_samples_to_int():
    info, _ = route_inputs(
        [_info_input({**INFO, 'n_samples': str(INFO['n_samples'])})], UPLOAD_ID
    )
    assert info['n_samples'] == INFO['n_samples']


def test_route_inputs_rejects_non_numeric_n_samples():
    with pytest.raises(HysprintInputError, match='whole number'):
        route_inputs([_info_input({**INFO, 'n_samples': 'many'})], UPLOAD_ID)


def test_assemble_builds_the_canonical_archive():
    slots = [
        {'step_type': 'Cleaning', 'variants': [{'samples': 'all', 'time': 5}]},
        {
            'step_type': 'Spin Coating',
            'variants': [
                {'samples': ['1'], 'material_name': 'NiOx'},
                {'samples': ['2'], 'material_name': 'PTAA'},
            ],
        },
    ]

    archive = assemble(INFO, slots)

    assert [s['lab_id'] for s in archive['samples']] == [
        'perov_B1_a_C-1',
        'perov_B1_a_C-2',
    ]
    assert [s['position_in_experimental_plan'] for s in archive['steps']] == [1, 2, 2]
    assert archive['steps'][0]['samples'] == 'all'
    assert archive['steps'][1]['samples'] == ['perov_B1_a_C-1']


# --- sample-label resolution: narrated labels -> declared sample names ---


def test_resolve_keeps_exact_matches():
    resolved = resolve_sample_labels(['s_1', 's_11'], ['s_1', 's_2', 's_11'])
    assert resolved == ['s_1', 's_11']


def test_resolve_maps_bare_numbers_to_prefixed_names():
    # the reported failure: form declared s_1..s_14, narration said "1"
    resolved = resolve_sample_labels(['1', '11'], [f's_{i}' for i in range(1, 15)])
    assert resolved == ['s_1', 's_11']


def test_resolve_normalizes_zero_padding_both_directions():
    padded = [f's_{i:02d}' for i in range(1, 15)]
    assert resolve_sample_labels(['1', '01', '001', 's_001'], padded) == ['s_01'] * 4

    plain = [f's_{i}' for i in range(1, 15)]
    assert resolve_sample_labels(['01', '001'], plain) == ['s_1', 's_1']


def test_resolve_matches_the_last_number_through_a_suffix():
    suffixed = [f's_{i}_x' for i in range(1, 15)]
    assert resolve_sample_labels(['1', '11', 's_1'], suffixed) == [
        's_1_x',
        's_11_x',
        's_1_x',
    ]
    # more numbers before the counter: the LAST one is the sample number
    assert resolve_sample_labels(['2', '3'], ['a_1_s_2_x', 'a_1_s_3_x']) == [
        'a_1_s_2_x',
        'a_1_s_3_x',
    ]


def test_resolve_rejects_unmatched_label():
    with pytest.raises(HysprintInputError, match="narrated sample '99'"):
        resolve_sample_labels(['99'], ['s_1', 's_2'])


def test_resolve_rejects_ambiguous_trailing_number():
    # two declared names share the trailing number: refuse, never guess
    with pytest.raises(HysprintInputError, match='cannot match'):
        resolve_sample_labels(['1'], ['a_1', 'b_1'])


def test_assemble_leaves_all_untouched():
    slots = [{'step_type': 'Cleaning', 'variants': [{'samples': 'all', 'time': 5}]}]
    archive = assemble(
        {**INFO, 'first_sample': 's_1'},
        slots,
    )
    assert archive['steps'][0]['samples'] == 'all'


def test_assemble_resolves_labels_through_to_lab_ids():
    info = {
        'project_name': 'p',
        'batch': 'b',
        'subbatch': 's',
        'first_sample': 's_1',
        'n_samples': 14,
    }
    slots = [
        {
            'step_type': 'Cleaning',
            'variants': [{'samples': ['1', 's_11'], 'time': 5}],
        }
    ]

    archive = assemble(info, slots)

    assert archive['steps'][0]['samples'] == ['p_b_s_C-s_1', 'p_b_s_C-s_11']
