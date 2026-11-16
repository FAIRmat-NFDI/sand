"""The reuse evaluation's chains and player, without a model: every
chain loads, and the player finds a reused answer that a fresh reading
would not give."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / 'evals' / 'sheet_edit'))

import run_chains as rc  # noqa: E402

from sand.sheet_edit.edits import Anchor, Edit, ProposedEdits, Range  # noqa: E402

CHAINS = rc.HERE / 'chains.json'
MIN_CHAINS = 12
SOLVENT = 'step 1 UV ozone cleaning, solvent 2 to DMSO for samples 1 to 3'
TIME = 'step 1 UV ozone cleaning, the time of the DMSO to 300 for samples 1 to 3'
SPEED = 'step 2 spin coating, rotation speed 3500 for all samples'
SPEED_NOTE = 3
FIRST_THREE = Range(first='1', last='3', subbatch=None)


def edit(target, rows, old, new, said, *, anchor=None):  # noqa: PLR0913
    return Edit(target=target, rows=rows, anchor=anchor, old=old, new=new, said=said)


ANSWERS = {
    SOLVENT: edit('C013', FIRST_THREE, 'IPA', 'DMSO', SOLVENT),
    TIME: edit(
        'C014',
        FIRST_THREE,
        '600',
        '300',
        TIME,
        anchor=Anchor(column='C013', equals='DMSO'),
    ),
    SPEED: edit('C025', 'all', '3000', '3500', SPEED),
}


class Canned:
    """The model as a dict; counts the readings."""

    def __init__(self, answers=None):
        self.answers = {**ANSWERS, **(answers or {})}
        self.calls = []

    def __call__(self, text, catalog):
        self.calls.append(text)
        return ProposedEdits(edits=[self.answers[text]], unresolved=[])


def chain(*clicks, notes=(SOLVENT, TIME, SPEED)):
    return rc.Chain('test', tuple(notes), tuple(rc.Click(**c) for c in clicks))


def test_every_chain_loads(synthetic_xlsx):
    chains = rc.load_chains(CHAINS)
    assert len(chains) >= MIN_CHAINS
    assert all(c.why and c.clicks for c in chains)


@pytest.mark.parametrize(
    ('clicks', 'error'),
    [
        ([{'do': 'confirm', 'note': 4}], 'names note 4, not there'),
        ([{'do': 'delete', 'note': 1}, {'do': 'undo', 'note': 1}], 'not there'),
        ([{'do': 'press', 'note': 1}], 'unknown click'),
        ([{'do': 'reword', 'note': 1}], 'takes a note and a text'),
        ([{'do': 'add', 'note': 1, 'text': 'x'}], 'takes a text'),
    ],
)
def test_a_click_on_no_note_is_refused_when_loading(tmp_path, clicks, error):
    path = tmp_path / 'chains.json'
    path.write_text(
        json.dumps(
            {'chains': [{'id': 'c', 'notes': ['a', 'b', 'c'], 'clicks': clicks}]}
        )
    )
    with pytest.raises(ValueError, match=error):
        rc.load_chains(path)


def test_a_chain_where_reuse_holds_passes(synthetic_xlsx):
    model = Canned()
    played = rc.play(
        chain(
            {'do': 'reject', 'note': 1},
            {'do': 'undo', 'note': 1},
            {'do': 'confirm', 'note': 1},
        ),
        synthetic_xlsx,
        model,
    )
    assert played.ok, rc.report([played])
    # the speed note names nothing that moved: reused, and compared
    assert any(c.note == SPEED_NOTE for c in played.compared)
    # the jobs read 3 notes, then the DMSO note once without its DMSO
    assert played.readings == 4  # noqa: PLR2004
    assert model.calls.count(TIME) == 2  # noqa: PLR2004


def test_a_reused_answer_a_fresh_reading_would_not_give_fails(synthetic_xlsx):
    fresh = Canned({SPEED: edit('C025', 'all', '3000', '3600', SPEED)})
    played = rc.play(
        chain({'do': 'reject', 'note': 1}), synthetic_xlsx, Canned(), fresh
    )
    assert not played.ok
    text = rc.report([played])
    assert 'note 3 DIFFERS' in text
    assert rc.summarize([played])['different'] >= 1


def test_a_click_out_of_turn_is_skipped(synthetic_xlsx):
    played = rc.play(
        chain({'do': 'confirm', 'note': 3}, {'do': 'undo', 'note': 1}),
        synthetic_xlsx,
        Canned(),
    )
    assert played.skipped == [
        'job 1, confirm 3: not its turn (open: note 1)',
        'job 2, undo 1: no rejection to undo',
    ]


def test_each_pass_is_kept_under_the_prompt_it_answers(synthetic_xlsx):
    from sand.sheet_edit.catalog import build

    names = []

    def call(name, system, text, schema):
        names.append(name)
        return json.dumps({'edits': [], 'unresolved': []})

    ask = rc.asking(call, 'c')
    catalog = build(synthetic_xlsx)
    ask(SPEED, catalog)
    ask(SPEED, catalog)
    ask(TIME, catalog)
    assert names[:2] == names[2:4]
    assert names[0].endswith('.first') and names[1].endswith('.second')
    assert len(set(names)) == 4  # noqa: PLR2004


def test_an_answer_reused_on_the_same_sheet_again_is_compared_once(synthetic_xlsx):
    fresh = Canned()
    played = rc.play(
        chain(
            {'do': 'reject', 'note': 1},
            {'do': 'undo', 'note': 2},  # skipped: no job, nothing new
            {'do': 'reword', 'note': 2, 'text': TIME + ' please'},
        ),
        synthetic_xlsx,
        Canned({TIME + ' please': ANSWERS[TIME]}),
        fresh,
    )
    sheets = [(c.note, c.reused) for c in played.compared]
    assert len(sheets) == len(set(sheets))
    assert fresh.calls.count(SPEED) == 1


def test_a_reading_an_earlier_answer_would_have_matched_is_careful(synthetic_xlsx):
    """The note names "3000", which the note above moves and puts back:
    read again, with the edits its first answer makes there."""
    above = 'step 2 spin coating, rotation speed 3500 for samples 1 to 5'
    said = 'step 2 spin coating, rotation speed from 3000 to 5000 for samples 7 to 13'
    model = Canned(
        {
            above: edit(
                'C025', Range(first='1', last='5', subbatch=None), '3000', '3500', above
            ),
            said: edit(
                'C025', Range(first='7', last='13', subbatch=None), '3000', '5000', said
            ).model_copy(update={'said_old': '3000'}),
        }
    )
    played = rc.play(
        chain(
            {'do': 'reject', 'note': 1},
            {'do': 'undo', 'note': 1},
            notes=(above, said),
        ),
        synthetic_xlsx,
        model,
    )
    assert played.ok, rc.report([played])
    assert played.careful >= 1


def test_reword_delete_and_add_play_like_the_page(synthetic_xlsx):
    added = 'step 2 spin coating, rotation speed 3600 for all samples'
    model = Canned({added: edit('C025', 'all', '3000', '3600', added)})
    played = rc.play(
        chain(
            {'do': 'confirm', 'note': 1},
            {'do': 'delete', 'note': 3},
            {'do': 'add', 'text': added},
            {'do': 'confirm', 'note': 2},
            {'do': 'confirm', 'note': 4},
        ),
        synthetic_xlsx,
        model,
    )
    # the added note is note 4, not the deleted note's 3
    assert played.skipped == [], played.skipped
    assert played.ok
    assert added in model.calls


def test_a_decision_matching_none_of_its_notes_edits_is_dropped():
    decisions = rc._Decisions(confirmed={'old': 2, 'k': 2, 'other': 3})

    class Step:
        class interpretation:  # noqa: N801
            ready = {'k': None}

    decisions.keep_only(2, Step)
    assert decisions.confirmed == {'k': 2, 'other': 3}
