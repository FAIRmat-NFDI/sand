import pytest

from sand.sheet_edit.catalog import build
from sand.sheet_edit.coverage import Coverage, coverage
from sand.sheet_edit.edits import (
    Ambiguous,
    Anchor,
    Answer,
    Edit,
    NotFound,
    Range,
    Samples,
)
from sand.sheet_edit.ground import ground
from sand.sheet_edit.preview import NOTHING, edit_lines, preview

SPEED, TIME, ANNEAL_TIME, VARIATION, NOTES = 'C025', 'C026', 'C027', 'C007', 'C029'
DATE, SUBSTRATE, CLEAN_SOLVENT_2, VOLUME_3, SOLVENT_3 = (
    'C001',
    'C055',
    'C013',
    'C034',
    'C033',
)
NONE = Coverage((), ())


@pytest.fixture(scope='module')
def catalog(synthetic_xlsx):
    return build(synthetic_xlsx)


def edit(target=SPEED, rows='all', **fields):
    fields = {
        'anchor': None,
        'old': '3000',
        'new': '4000',
        'said': 'speed 4000',
        **fields,
    }
    return Edit(target=target, rows=rows, **fields)


def lines(catalog, *edits, unresolved=(), instruction=None):
    """The preview of ground + coverage on these edits, as lines."""
    results = [ground(e, catalog) for e in edits]
    answer = Answer(edits=list(edits), unresolved=list(unresolved))
    cov = coverage(instruction, answer, catalog) if instruction else NONE
    return preview(results, list(unresolved), cov, catalog).split('\n')


def test_an_edit_reads_as_step_header_change_samples_and_words(catalog):
    assert lines(catalog, edit()) == [
        '2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, all 12 samples',
        '   from "speed 4000"',
    ]


def test_the_whole_outcome_of_one_instruction(catalog):
    said = (
        'solvent 2 to DMSO for samples 1 to 3, speed four thousand, and anneal at 120'
    )
    solvent = edit(
        CLEAN_SOLVENT_2,
        Range(first='1', last='3', subbatch=None),
        old='IPA',
        new='DMSO',
        said='solvent 2 to DMSO for samples 1 to 3',
    )
    speed = edit(said='speed four thousand')
    item = NotFound(reason='not_found', said='anneal at 120')
    assert lines(catalog, solvent, speed, unresolved=[item], instruction=said) == [
        '1: Cleaning UV-Ozone · Solvent 2: IPA → DMSO, samples 1–3',
        '   from "solvent 2 to DMSO for samples 1 to 3"',
        '   · new term: DMSO',
        '2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, all 12 samples',
        '   from "speed four thousand"',
        "   · '4000': read from 'four thousand'",
        '? not found: "anneal at 120"',
    ]


def test_a_refused_edit_lists_its_reasons(catalog):
    assert lines(catalog, edit(new='4100')) == [
        '✗ not applied: "speed 4000"',
        "   Rotation speed [rpm] (2: Spin Coating): '4100' is not in your words; "
        'you said 4000',
    ]


def test_nothing_to_change(catalog):
    assert lines(catalog, edit(new='3000', said='speed 3000')) == [
        '= 2: Spin Coating · Rotation speed [rpm]',
        '   from "speed 3000"',
        '   · already 3000 on all 12 samples, nothing to change',
    ]
    assert lines(catalog, edit(NOTES, old=None, new='', said='clear the notes')) == [
        '= 2: Spin Coating · Notes',
        '   from "clear the notes"',
        '   · already empty on all 12 samples, nothing to change',
    ]


def test_rows_already_at_the_value_are_set_aside(catalog):
    said = 'substrate temperature 115'
    assert lines(catalog, edit(SUBSTRATE, old='120', new='115', said=said)) == [
        '5: Close Space Sublimation · Substrate temperature [°C]: 120 → 115, '
        'samples 1–5, 7',
        '   from "substrate temperature 115"',
        '   · already 115 on samples 8–13, nothing to change',
    ]


def test_a_gap_in_a_range_is_a_note(catalog):
    said = 'samples 1 to 8 speed 4000'
    rows = Range(first='1', last='8', subbatch=None)
    assert lines(catalog, edit(rows=rows, said=said)) == [
        '2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, samples 1–5, 7–8',
        '   from "samples 1 to 8 speed 4000"',
        '   · samples 1 to 8: no sample 6, applied to 7 samples',
    ]


def test_a_doubt_is_a_question(catalog):
    said = 'set the usual speed for samples 1 to 3'
    rows = Samples(samples=['1', '2', '3'], subbatch=None)
    assert lines(catalog, edit(rows=rows, said=said)) == [
        '2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, samples 1–3',
        '   from "set the usual speed for samples 1 to 3"',
        "   ? '4000' not found in your words",
    ]


def test_a_formula_reading_the_column_is_a_mark(catalog):
    said = 'variation 20min for samples 1 to 3'
    rows = Samples(samples=['1', '2', '3'], subbatch=None)
    variation = edit(VARIATION, rows, old='10min', new='20min', said=said)
    assert lines(catalog, variation) == [
        'Experiment Info · Variation: 10min → 20min, samples 1–3',
        '   from "variation 20min for samples 1 to 3"',
        '   · changes 5: Close Space Sublimation · Deposition Time [s] by formula',
    ]
    date = edit(DATE, old='20250101', new='20250301', said='date 20250301')
    assert lines(catalog, date)[0] == (
        'Experiment Info · Date: 20250101 → 20250301, all 12 samples'
    )
    assert (
        lines(catalog, date)[-1] == '   · changes Experiment Info · Nomad ID by formula'
    )


def test_a_text_no_cell_holds_is_a_new_term(catalog):
    said = 'variation 30min for samples 1 to 3'
    rows = Samples(samples=['1', '2', '3'], subbatch=None)
    variation = edit(VARIATION, rows, old='10min', new='30min', said=said)
    assert '   · new term: 30min' in lines(catalog, variation)
    # '20min' is in the sheet already: no mark
    variation = edit(VARIATION, rows, old='10min', new='20min', said=said)
    assert not any('new term' in line for line in lines(catalog, variation))


def test_an_anchored_edit_reads_like_any_other(catalog):
    said = 'the volume of the Ethanol to 100'
    volume = edit(
        VOLUME_3,
        anchor=Anchor(column=SOLVENT_3, equals='Ethanol'),
        old='50',
        new='100',
        said=said,
    )
    assert lines(catalog, volume)[0] == (
        '3: Spin Coating · Solvent 1 volume [uL]: 50 → 100, all 12 samples'
    )


def test_unresolved_items(catalog):
    item = Ambiguous(reason='ambiguous', candidates=[TIME, ANNEAL_TIME], said='time 40')
    assert lines(catalog, unresolved=[item]) == [
        '? ambiguous: "time 40"',
        '   2: Spin Coating · Rotation time [s] or 2: Spin Coating · Annealing time '
        '[min]?',
    ]
    item = Ambiguous(reason='ambiguous', candidates=['C999'], said='time 40')
    assert lines(catalog, unresolved=[item])[1] == '   C999?'


def test_what_nothing_used_comes_last(catalog):
    said = 'speed 4000 and time 40'
    assert lines(catalog, edit(said=said), instruction=said) == [
        '2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, all 12 samples',
        '   from "speed 4000 and time 40"',
        '⚠ not applied: "time 40"',
    ]
    # a misquoted edit still reads as an edit: replay (PR 14) drops it
    assert lines(catalog, edit(said='speed to 4000'), instruction='speed 4000') == [
        '2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, all 12 samples',
        '   from "speed to 4000"',
        '⚠ not applied: "speed 4000"',
        '⚠ misquoted: "speed to 4000"',
    ]


def test_nothing_at_all(catalog):
    assert preview([], [], NONE, catalog) == NOTHING


def test_one_edit_alone(catalog):
    assert edit_lines(ground(edit(), catalog), catalog) == [
        '2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, all 12 samples',
        '   from "speed 4000"',
    ]
