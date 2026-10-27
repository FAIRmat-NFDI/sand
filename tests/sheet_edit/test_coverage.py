import pytest
from synthetic_sheet import edited, letter

from sand.sheet_edit.catalog import build
from sand.sheet_edit.coverage import Coverage, coverage
from sand.sheet_edit.edits import (
    Ambiguous,
    Anchor,
    Edit,
    NotFound,
    ProposedEdits,
    Range,
    Samples,
    Where,
)

INFO = 'Experiment Info'
SPEED, TIME, VARIATION = 'C025', 'C026', 'C007'
CLEAN_SOLVENT_2, VOLUME_3, SOLVENT_3 = 'C013', 'C034', 'C033'


@pytest.fixture(scope='module')
def catalog(synthetic_xlsx):
    return build(synthetic_xlsx)


@pytest.fixture(scope='module')
def words(synthetic_xlsx):
    sample = letter(INFO, 'Sample')
    names = ['A', 'B', 'C', 'D', 'E', 'ref', 'hot', 'cold', 'Ctrl', 'Ctrl-2', 'x', 'K']

    def change(ws):
        for r, name in zip(range(3, 15), names, strict=True):
            ws[f'{sample}{r}'] = name

    return build(edited(synthetic_xlsx, change))


def edit(target=SPEED, rows='all', **fields):
    fields = {
        'anchor': None,
        'old': '3000',
        'new': '4000',
        'said': 'speed 4000',
        **fields,
    }
    return Edit(target=target, rows=rows, **fields)


def time(said='time 40'):
    return edit(TIME, old='30', new='40', said=said)


def not_found(said):
    return NotFound(reason='not_found', said=said)


def check(instruction, catalog, *edits, unresolved=()):
    proposal = ProposedEdits(edits=list(edits), unresolved=list(unresolved))
    return coverage(instruction, proposal, catalog)


def not_applied(instruction, catalog, *edits, unresolved=()):
    result = check(instruction, catalog, *edits, unresolved=unresolved)
    assert result.misquoted == ()
    return result.not_applied


# --- dropped clauses ----------------------------------------------------------


def test_a_number_no_edit_uses_is_not_applied(catalog):
    # the plan's case: one edit for two changes
    said = 'speed 4000 and time 40'
    assert not_applied(said, catalog, edit(said=said)) == ('time 40',)
    assert not_applied(said, catalog, edit(), time()) == ()


def test_two_edits_may_quote_the_same_words(catalog):
    said = 'speed 4000 and time 40'
    assert not_applied(said, catalog, edit(said=said), time(said=said)) == ()


def test_a_clause_outside_every_edit_is_not_applied(catalog):
    said = 'set solvent 2 to DMSO for samples 1 to 3, speed 4000, and anneal at 120'
    solvent = edit(
        CLEAN_SOLVENT_2,
        Range(first='1', last='3', subbatch=None),
        old='IPA',
        new='DMSO',
        said='solvent 2 to DMSO for samples 1 to 3',
    )
    assert not_applied(said, catalog, solvent, edit()) == ('anneal at 120',)
    # reported as unresolved: the user sees it, nothing is dropped
    item = not_found('anneal at 120')
    assert not_applied(said, catalog, solvent, edit(), unresolved=[item]) == ()


def test_sample_numbers_must_reach_the_rows(catalog):
    # rows 'all' with the samples quoted: ground refuses; the samples
    # outside the quoted words: coverage reports them
    assert not_applied('samples 1 to 3 speed 4000', catalog, edit()) == (
        'samples 1 to 3',
    )
    rows = Range(first='1', last='3', subbatch=None)
    said = 'samples 1 to 3 speed 4000'
    assert not_applied(said, catalog, edit(rows=rows, said=said)) == ()


def test_a_sheet_word_outside_every_edit_is_not_applied(catalog):
    assert not_applied('the hot samples speed 4000', catalog, edit()) == (
        'the hot samples',
    )
    rows = Where(where=VARIATION, equals='10min-hot')
    said = 'the hot samples speed 4000'
    assert not_applied(said, catalog, edit(rows=rows, said=said)) == ()


def test_a_sheet_value_no_field_holds_is_not_applied(catalog):
    said = 'speed 4000 and Ethanol wash'
    assert not_applied(said, catalog, edit(said=said)) == ('Ethanol wash',)
    said = 'speed 4000 and Ag'  # the Evaporation material, as a whole word
    assert not_applied(said, catalog, edit(said=said)) == ('Ag',)
    assert not_applied('speed 4000 again', catalog, edit(said='speed 4000 again')) == ()


def test_words_the_sheet_does_not_know_are_ignored(catalog):
    said = 'change the speed from 3000 to 4000 quickly'
    assert (
        not_applied(said, catalog, edit(said='change the speed from 3000 to 4000'))
        == ()
    )
    assert not_applied('please, speed 4000, thanks', catalog, edit()) == ()


def test_a_word_names_the_step_it_starts(catalog):
    # 'anneal' is the start of 'Annealing'; 'ann' is too short to say so
    assert not_applied('speed 4000, anneal too', catalog, edit()) == ('anneal too',)
    assert not_applied('speed 4000, ann too', catalog, edit()) == ()


def test_the_report_stops_at_what_the_edit_used(catalog):
    said = 'speed 4000 rpm for 30 seconds'
    assert not_applied(said, catalog, edit(said=said)) == ('for 30 seconds',)
    said = 'set 2 to 4000'  # no header word: the 2 is not a step number
    assert not_applied(said, catalog, edit(said=said)) == ('set 2 to',)


def test_each_line_is_a_clause(catalog):
    assert not_applied('speed 4000\ntime 40', catalog, edit()) == ('time 40',)


def test_the_dropped_clause_is_reported_once(catalog):
    # a number and a sheet word in the same clause: one line
    said = 'speed 4000, annealing temperature 120'
    assert not_applied(said, catalog, edit()) == ('annealing temperature 120',)


def test_nothing_quoted_means_nothing_applied(catalog):
    assert not_applied('speed 4000', catalog) == ('speed 4000',)
    assert check('', catalog) == Coverage((), ())


# --- what an edit uses ----------------------------------------------------------


def test_the_numbers_of_the_column_name_are_used(catalog):
    for said in ('spin coating 2 speed 4000', 'step 2 set the speed to 4000 rpm'):
        assert not_applied(said, catalog, edit(said=said)) == ()


def test_old_and_new_and_the_anchor_are_used(catalog):
    said = 'change the speed from 3000 to 4000'
    assert not_applied(said, catalog, edit(said=said)) == ()
    said = 'the volume of the Ethanol to 100'
    volume = edit(
        VOLUME_3, anchor=Anchor(column=SOLVENT_3, equals='Ethanol'), old='50', new='100'
    )
    assert not_applied(said, catalog, volume.model_copy(update={'said': said})) == ()


def test_a_spoken_number_is_used_by_its_digits(catalog):
    said = 'speed four thousand'
    assert not_applied(said, catalog, edit(said=said)) == ()


def test_the_rows_words_are_used(catalog):
    said = 'the 10min samples: speed 4000'
    rows = Where(where=VARIATION, equals='10min')
    assert not_applied(said, catalog, edit(rows=rows, said=said)) == ()
    said = 'samples 1, 3 and 7 to 9 speed 4000'
    rows = Samples(samples=['1', '3', '7', '8', '9'], subbatch=None)
    assert not_applied(said, catalog, edit(rows=rows, said=said)) == ()
    said = 'sample no. 3 speed 4000'
    rows = Samples(samples=['3'], subbatch=None)
    assert not_applied(said, catalog, edit(rows=rows, said=said)) == ()


def test_a_number_inside_a_value_is_used_by_it(catalog):
    said = 'set variation to 20min-hot for samples 1 and 2'
    variation = edit(
        VARIATION,
        Samples(samples=['1', '2'], subbatch=None),
        old='10min',
        new='20min-hot',
        said=said,
    )
    assert not_applied(said, catalog, variation) == ()


def test_a_number_no_field_holds_is_not_applied(catalog):
    # the value check refuses this edit; its clause is reported too
    said = 'speed 4100'
    assert not_applied(said, catalog, edit(said=said)) == ('speed 4100',)


def test_an_unknown_target_covers_nothing_but_its_quote(catalog):
    said = 'samples 1 to 3 speed 4000'
    rows = Range(first='1', last='3', subbatch=None)
    assert not_applied(said, catalog, edit('C999', rows=rows, said=said)) == ()


def test_word_labels_are_sheet_values(words):
    assert not_applied('ref and Ctrl-2 speed 4000', words, edit()) == ('ref', 'Ctrl-2')
    rows = Samples(samples=['ref', 'Ctrl-2'], subbatch=None)
    said = 'ref and Ctrl-2 speed 4000'
    assert not_applied(said, words, edit(rows=rows, said=said)) == ()
    # single-letter labels are letters: 'a' in 'a speed of 4000' is not sample A
    assert not_applied('a speed of 4000', words, edit(said='a speed of 4000')) == ()


# --- misquoted ------------------------------------------------------------------


def test_an_edit_must_quote_the_instruction(catalog):
    result = check('spin coating 2 speed 4000', catalog, edit(said='speed to 4000'))
    assert result.misquoted == ('speed to 4000',)
    assert result.not_applied == ('spin coating 2 speed 4000',)  # it covers nothing
    assert not result.complete


def test_case_spacing_and_end_punctuation_do_not_count(catalog):
    result = check(
        'Spin  Coating 2 speed 4000.', catalog, edit(said='spin coating 2 speed 4000')
    )
    assert result == Coverage((), ())


def test_an_unresolved_item_must_quote_the_instruction_too(catalog):
    item = Ambiguous(reason='ambiguous', candidates=[SPEED], said='the speed thing')
    result = check('speed 4000', catalog, unresolved=[item])
    assert result.misquoted == ('the speed thing',)
    assert (
        not_applied('the speed 4000', catalog, unresolved=[not_found('speed 4000')])
        == ()
    )
