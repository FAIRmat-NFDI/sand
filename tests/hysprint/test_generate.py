import json

import pytest

from sand.hysprint.generate import HysprintInputError, assemble, route_inputs
from sand.services.voice_eln import CollectedInput

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
    return _input('n-info', json.dumps(info), label='experiment_info')


def test_route_inputs_separates_info_from_ordered_steps():
    inputs = [
        _info_input(),
        _input('a-1', 'cleaned the substrates', kind='audio'),
        _input('n-1', 'spin coated NiOx'),
    ]

    info, steps = route_inputs(inputs)

    assert info == INFO
    assert steps == ['cleaned the substrates', 'spin coated NiOx']


def test_route_inputs_requires_experiment_info():
    with pytest.raises(HysprintInputError, match='experiment_info'):
        route_inputs([_input('n-1', 'a step')])


def test_route_inputs_rejects_invalid_info_json():
    inputs = [_input('n-info', 'not json', label='experiment_info')]
    with pytest.raises(HysprintInputError, match='not valid JSON'):
        route_inputs(inputs)


def test_route_inputs_rejects_incomplete_info():
    inputs = [
        _info_input({'project_name': 'perov'}),
        _input('n-1', 'a step'),
    ]
    with pytest.raises(HysprintInputError, match='missing fields'):
        route_inputs(inputs)


def test_route_inputs_rejects_textless_input():
    inputs = [_info_input(), _input('a-1', None, kind='audio')]
    with pytest.raises(HysprintInputError, match='no text yet'):
        route_inputs(inputs)


def test_route_inputs_requires_at_least_one_step():
    with pytest.raises(HysprintInputError, match='no step inputs'):
        route_inputs([_info_input()])


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
