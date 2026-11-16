import pytest

from sand.sheet_edit.catalog import build
from sand.sheet_edit.edits import (
    Anchor,
    Edit,
    NotFound,
    ProposedEdits,
    Range,
    Samples,
    Where,
)
from sand.sheet_edit.reads import from_json, holds, reads_of, to_json
from sand.sheet_edit.workbook import sha256
from sand.sheet_edit.write import Write, write_cells

SAMPLE, VARIATION, NOTES = 'C005', 'C007', 'C009'
SOLVENT_1, SOLVENT_2, TIME_2 = 'C010', 'C013', 'C014'
SPEED, ROTATION_TIME, SPIN_NOTES = 'C025', 'C026', 'C029'
FIRST_THREE = Range(first='1', last='3', subbatch=None)
ROWS_1_TO_3 = (3, 4, 5)


def edit(target, rows, old, new, said, **fields):
    return Edit(
        target=target,
        rows=rows,
        old=old,
        new=new,
        said=said,
        **{
            'anchor': None,
            **fields,
        },
    )


def one(*edits, unresolved=()):
    return ProposedEdits(edits=list(edits), unresolved=list(unresolved))


def written(xlsx, *cells):
    """The sheet with (column id, rows, value) written."""
    catalog = build(xlsx)
    writes = [
        Write(row, catalog.index(column), value)
        for column, rows, value in cells
        for row in rows
    ]
    return write_cells(xlsx, writes).xlsx


def check(read_on, text, proposal, now):
    """Whether the answer read on `read_on` holds on `now`."""
    reads = reads_of(text, proposal, build(read_on), sha256(read_on))
    return holds(reads, text, proposal, build(now))


TIME = 'the time of the DMSO for samples 1 to 3 to 300'
ANCHORED = one(
    edit(
        TIME_2,
        FIRST_THREE,
        '600',
        '300',
        TIME,
        anchor=Anchor(column=SOLVENT_2, equals='DMSO'),
    )
)


@pytest.fixture(scope='module')
def with_dmso(synthetic_xlsx):
    return written(synthetic_xlsx, (SOLVENT_2, ROWS_1_TO_3, 'DMSO'))


def test_an_answer_holds_while_what_it_names_sits_where_it_was(with_dmso):
    now = written(with_dmso, (SPEED, ROWS_1_TO_3, 4000))
    assert check(with_dmso, TIME, ANCHORED, now) is True


def test_an_anchor_value_gone_breaks_the_answer(synthetic_xlsx, with_dmso):
    assert check(with_dmso, TIME, ANCHORED, synthetic_xlsx) is False


def test_an_anchor_value_also_elsewhere_breaks_the_answer(with_dmso):
    """Finding B: the old answer would pass every check, a new reading
    would be ambiguous."""
    now = written(with_dmso, (SOLVENT_1, ROWS_1_TO_3, 'DMSO'))
    assert check(with_dmso, TIME, ANCHORED, now) is False


def test_where_rows_changed_break_the_answer(with_results):
    said = 'speed 5000 for the 20min samples'
    proposal = one(
        edit(SPEED, Where(where=VARIATION, equals='20min'), '3000', '5000', said)
    )
    now = written(with_results, (VARIATION, (3,), '20min'))
    assert check(with_results, said, proposal, now) is False


def test_rows_picked_by_an_empty_cell_break_when_it_is_filled(synthetic_xlsx):
    said = 'speed 5000 where there are no notes'
    proposal = one(edit(SPEED, Where(where=NOTES, equals=None), '3000', '5000', said))
    now = written(synthetic_xlsx, (NOTES, (3,), 'cracked'))
    assert check(synthetic_xlsx, said, proposal, now) is False


def test_the_old_value_the_user_said_moved(synthetic_xlsx):
    said = 'speed 5000 for samples 1 to 3 from 3000'
    proposal = one(edit(SPEED, FIRST_THREE, '3000', '5000', said, said_old='3000'))
    now = written(synthetic_xlsx, (SPEED, (12,), 4000))
    assert check(synthetic_xlsx, said, proposal, now) is False


def test_a_text_value_of_the_words_new_in_the_sheet_breaks_the_answer(
    synthetic_xlsx,
):
    said = 'solvent 1 of the spin coating to DMF'
    proposal = one(edit('C020', 'all', 'Ethanol', 'DMF', said))
    now = written(synthetic_xlsx, (SOLVENT_1, (3,), 'DMF'))
    assert check(synthetic_xlsx, said, proposal, now) is False


def test_a_changed_sample_label_breaks_every_answer(with_results):
    said = 'speed 4000 for samples 1 to 3'
    proposal = one(edit(SPEED, FIRST_THREE, '3000', '4000', said))
    now = written(with_results, (SAMPLE, (3,), 21))
    assert check(with_results, said, proposal, now) is False


def test_numbers_that_are_the_new_value_or_samples_name_nothing(synthetic_xlsx):
    said = 'speed 4000 for samples 1 to 3'
    proposal = one(edit(SPEED, FIRST_THREE, '3000', '4000', said))
    reads = reads_of(said, proposal, build(synthetic_xlsx), sha256(synthetic_xlsx))
    assert reads.held == ()
    now = written(synthetic_xlsx, (SPEED, ROWS_1_TO_3, 4000))
    assert check(synthetic_xlsx, said, proposal, now) is True


def test_a_number_of_the_words_that_is_not_the_new_value_is_named(synthetic_xlsx):
    """Question 8: "the 30" picks the cells holding 30."""
    said = 'change the rotation time 30 to 45'
    proposal = one(edit(ROTATION_TIME, 'all', '30', '45', said))
    now = written(synthetic_xlsx, (ROTATION_TIME, ROWS_1_TO_3, 40))
    assert check(synthetic_xlsx, said, proposal, now) is False


def test_a_cell_written_while_empty_must_still_be_empty(synthetic_xlsx):
    """Question 7: the model may have picked it for being empty."""
    said = 'spin coating notes: cloudy for samples 1 to 3'
    proposal = one(edit(SPIN_NOTES, FIRST_THREE, None, 'cloudy', said))
    now = written(synthetic_xlsx, (SPIN_NOTES, (4,), 'cracked'))
    assert check(synthetic_xlsx, said, proposal, now) is False


@pytest.mark.parametrize(
    ('said', 'proposal'),
    [
        # a part the model could not place
        (
            'speed 4000 and anneal',
            one(
                edit(SPEED, 'all', '3000', '4000', 'speed 4000'),
                unresolved=[NotFound(reason='not_found', said='anneal')],
            ),
        ),
        # a value the model worked out
        (
            'speed 10 percent higher',
            one(edit(SPEED, 'all', '3000', '3300', 'speed 10 percent higher')),
        ),
        # pointed through a value the user did not say
        (
            'the time of the solvent to 300',
            one(
                edit(
                    TIME_2,
                    'all',
                    '600',
                    '300',
                    'the time of the solvent to 300',
                    anchor=Anchor(column=SOLVENT_2, equals='IPA'),
                )
            ),
        ),
        # words the answer left out
        (
            'speed 4000 and time 40',
            one(edit(SPEED, 'all', '3000', '4000', 'speed 4000')),
        ),
    ],
)
def test_some_answers_hold_only_on_the_sheet_they_were_read_on(
    synthetic_xlsx, said, proposal
):
    reads = reads_of(said, proposal, build(synthetic_xlsx), sha256(synthetic_xlsx))
    assert reads.whole == sha256(synthetic_xlsx)
    now = written(synthetic_xlsx, (NOTES, (3,), 'x'))
    assert check(synthetic_xlsx, said, proposal, now) is False


def test_a_read_set_survives_json(with_dmso):
    reads = reads_of(TIME, ANCHORED, build(with_dmso), sha256(with_dmso))
    assert reads.held
    assert from_json(to_json(reads)) == reads


def test_two_samples_named_by_label_stay_named(synthetic_xlsx):
    said = 'speed 4000 for samples 1 and 3'
    proposal = one(
        edit(SPEED, Samples(samples=['1', '3'], subbatch=None), '3000', '4000', said)
    )
    now = written(synthetic_xlsx, (SPEED, (4,), 5000))
    assert check(synthetic_xlsx, said, proposal, now) is True


def test_a_number_like_text_value_of_the_words_is_named(with_results):
    """'20min' is a text cell; the coverage check reads it as a number."""
    said = 'change the 20min samples to 25min'
    proposal = one(
        edit(
            VARIATION,
            Samples(samples=['4', '5', '7'], subbatch=None),
            '20min',
            '25min',
            said,
        )
    )
    now = written(with_results, (VARIATION, (3,), '20min'))
    assert check(with_results, said, proposal, now) is False


def test_a_column_chosen_because_its_sibling_was_empty(synthetic_xlsx):
    """'the solvent time' is Time 1 while Solvent 2 is empty."""
    said = 'the solvent time to 300 for all samples'
    proposal = one(edit('C011', 'all', '600', '300', said))
    read_on = written(synthetic_xlsx, (SOLVENT_2, range(3, 15), None))
    now = written(read_on, (SOLVENT_2, (3,), 'IPA'))
    assert check(read_on, said, proposal, now) is False
