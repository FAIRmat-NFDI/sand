from datetime import date

import pytest

from sand.hysprint.archive import append_step, build_samples, canonicalize

INFO = {
    'project_name': 'perov',
    'batch': 'B1',
    'subbatch': 'a',
    'first_sample': '1',
    'n_samples': 3,
    'date': '2026-08-27',
}


def test_build_samples_generates_names_and_lab_ids():
    samples = build_samples(INFO)
    assert [s['sample'] for s in samples] == ['1', '2', '3']
    assert samples[0]['lab_id'] == 'perov_B1_a_C-1'
    # ISO string: the format the hysprint batch parser accepts
    assert samples[0]['date'] == '2026-08-27'


def test_build_samples_defaults_date_to_today():
    samples = build_samples({**INFO, 'date': None})
    assert samples[0]['date'] == date.today().isoformat()


def test_build_samples_keeps_zero_padded_names():
    samples = build_samples({**INFO, 'first_sample': '05'})
    assert [s['sample'] for s in samples] == ['05', '06', '07']
    assert samples[0]['lab_id'] == 'perov_B1_a_C-05'


def test_build_samples_counts_the_last_number_and_keeps_the_suffix():
    samples = build_samples({**INFO, 'first_sample': 's_1_x'})
    assert [s['sample'] for s in samples] == ['s_1_x', 's_2_x', 's_3_x']

    samples = build_samples({**INFO, 'first_sample': 'a_1_s_2_x'})
    assert [s['sample'] for s in samples] == ['a_1_s_2_x', 'a_1_s_3_x', 'a_1_s_4_x']


def test_build_samples_rejects_uncountable_first_sample():
    with pytest.raises(ValueError, match='no number'):
        build_samples({**INFO, 'first_sample': 'alpha'})


def test_append_step_maps_labels_to_lab_ids():
    archive = {'samples': build_samples(INFO), 'steps': []}
    slot = {
        'step_type': 'Spin Coating',
        'variants': [
            {'samples': ['1', '2'], 'material_name': 'NiOx'},
            {'samples': ['3'], 'material_name': 'PTAA'},
        ],
    }
    append_step(archive, slot, 1)
    assert archive['steps'][0]['samples'] == ['perov_B1_a_C-1', 'perov_B1_a_C-2']
    assert archive['steps'][1]['samples'] == ['perov_B1_a_C-3']
    assert archive['steps'][0]['position_in_experimental_plan'] == 1


def test_append_step_rejects_undeclared_label():
    archive = {'samples': build_samples(INFO), 'steps': []}
    slot = {'step_type': 'Cleaning', 'variants': [{'samples': ['7']}]}
    with pytest.raises(ValueError, match="sample '7'"):
        append_step(archive, slot, 1)


def test_append_step_rejects_empty_samples_list():
    # samples: [] would land the step on no sample row: blank sheet block,
    # parser skips it - a complete-looking sheet with the step missing
    archive = {'samples': build_samples(INFO), 'steps': []}
    slot = {'step_type': 'Cleaning', 'variants': [{'samples': [], 'time': 5}]}
    with pytest.raises(ValueError, match='names no samples'):
        append_step(archive, slot, 1)


def test_canonicalize_merges_identical_variants_and_collapses_to_all():
    archive = {'samples': build_samples(INFO), 'steps': []}
    slot = {
        'step_type': 'Cleaning',
        'variants': [
            {'samples': ['1', '2'], 'time': 5},
            {'samples': ['3'], 'time': 5},
        ],
    }
    append_step(archive, slot, 1)
    result = canonicalize(archive)
    assert len(result['steps']) == 1
    assert result['steps'][0]['samples'] == 'all'


def test_canonicalize_requires_positions():
    archive = {'samples': [], 'steps': [{'step_type': 'Cleaning'}]}
    with pytest.raises(ValueError, match='position_in_experimental_plan'):
        canonicalize(archive)
