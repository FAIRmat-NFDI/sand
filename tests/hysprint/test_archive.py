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
    # ISO date converted to the spreadsheet serial
    assert samples[0]['date'] == (date(2026, 8, 27) - date(1899, 12, 30)).days


def test_build_samples_defaults_date_to_today_serial():
    samples = build_samples({**INFO, 'date': None})
    assert samples[0]['date'] == (date.today() - date(1899, 12, 30)).days


def test_build_samples_rejects_uncountable_first_sample():
    with pytest.raises(ValueError, match='no trailing number'):
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
