import dataclasses
import json

import pytest
from synthetic_sheet import edited, letter

from sand.sheet_edit.apply import interpret_instruction
from sand.sheet_edit.catalog import build
from sand.sheet_edit.coverage import Coverage
from sand.sheet_edit.edits import (
    Ambiguous,
    Disputed,
    Edit,
    NotFound,
    ProposedEdits,
    Range,
    Unreadable,
    Where,
    proposed_edits_schema,
)
from sand.sheet_edit.preview import preview
from sand.sheet_edit.render import table
from sand.sheet_edit.resolve import (
    PROMPT_VERSION,
    _field,
    agree,
    known_step,
    prompts,
    read_answer,
    resolve,
    system_prompt,
    user_text,
)

SPEED, ANNEAL_TIME, SOLVENT_2, VARIATION, TIME_2 = (
    'C025',
    'C027',
    'C013',
    'C007',
    'C014',
)
CLEAN_SOLVENT_1 = 'C010'  # 1: Cleaning UV-Ozone, Acetone on every sample
SUBSTRATE_TEMP = 'C055'  # 5: CSS, 120 on samples 1-7, 115 on 8-13
SAID = 'speed 4000 for samples 1 to 3'
NOTHING = Coverage((), ())


@pytest.fixture(scope='module')
def catalog(synthetic_xlsx):
    return build(synthetic_xlsx)


@pytest.fixture(scope='module')
def two_subbatches(synthetic_xlsx):
    subbatch, sample = (
        letter('Experiment Info', 'Subbatch'),
        letter('Experiment Info', 'Sample'),
    )

    def change(ws):  # rows 9-14 become subbatch 1, samples 1-6
        for i, r in enumerate(range(9, 15)):
            ws[f'{subbatch}{r}'] = 1
            ws[f'{sample}{r}'] = i + 1

    return build(edited(synthetic_xlsx, change))


def edit(**fields):
    base = {
        'target': SPEED,
        'rows': 'all',
        'anchor': None,
        'old': '3000',
        'unit': None,
        'said_old': None,
        'new': '4000',
        'said': SAID,
    }
    return {**base, **fields}


def answer(*edits, unresolved=()):
    return json.dumps({'edits': list(edits), 'unresolved': list(unresolved)})


_SAME = object()


class FakeCall:
    """Pass A gets `a`, pass B gets `b` (or the same); records what it saw."""

    def __init__(self, a, b=_SAME):
        self.answers = [a, a if b is _SAME else b]
        self.seen: list[tuple[str, str, dict]] = []

    def __call__(self, system: str, text: str, schema: dict):
        self.seen.append((system, text, schema))
        return self.answers[len(self.seen) - 1]


def ask(catalog, a, b=_SAME, instruction=SAID):
    return resolve(instruction, catalog, FakeCall(a, b))


def only(proposal, kind):
    (item,) = proposal.unresolved
    assert isinstance(item, kind)
    return item


# --- what the model is asked ------------------------------------------------


def test_two_passes_see_different_prompts_and_the_same_schema(catalog):
    call = FakeCall(answer())
    resolve(SAID, catalog, call)
    (system_a, text_a, schema_a), (system_b, text_b, schema_b) = call.seen
    assert system_a == system_prompt(second_opinion=False)
    assert system_b == system_prompt(second_opinion=True)
    assert system_a != system_b
    assert schema_a == schema_b == proposed_edits_schema(catalog)
    assert text_a.endswith(f'\nInstruction: {SAID}')
    assert text_b.endswith(f'\nInstruction: {SAID}')
    # pass B reads the steps last to first, columns within a step in order
    steps_a = _steps(text_a)
    assert _steps(text_b) == steps_a[::-1]
    assert steps_a[0] == 'Experiment Info'
    assert text_a.index('| C001 |') < text_a.index('| C002 |')
    assert text_b.index('| C001 |') < text_b.index('| C002 |')


def _steps(text: str) -> list[str]:
    steps = [
        line.split(' | ')[1] for line in text.splitlines() if line.startswith('| C')
    ]
    return list(dict.fromkeys(steps))


def test_two_steps_with_the_same_label_stay_two_steps_when_reversed(catalog):
    third = next(
        c.column.section
        for c in catalog.columns
        if c.column.section.label.startswith('3:')
    )
    second = next(
        c.column.section
        for c in catalog.columns
        if c.column.section.label.startswith('2:')
    )
    relabelled = dataclasses.replace(third, label=second.label)
    columns = tuple(
        dataclasses.replace(c, column=dataclasses.replace(c.column, section=relabelled))
        if c.column.section == third
        else c
        for c in catalog.columns
    )
    twice = dataclasses.replace(catalog, columns=columns)
    ids = [
        line.split(' | ')[0].strip('| ')
        for line in table(twice, reverse=True).splitlines()
    ]
    ids = [i for i in ids if i.startswith('C')]
    of_third = [c.id for c in twice.columns if c.column.section == relabelled]
    of_second = [c.id for c in twice.columns if c.column.section == second]
    assert ids.index(of_third[-1]) < ids.index(of_second[0])  # step 3 before step 2
    assert ids[ids.index(of_third[0]) : ids.index(of_third[-1]) + 1] == of_third


def test_both_passes_keep_the_anchor_to_the_users_value():
    for second in (False, True):
        prompt = ' '.join(system_prompt(second_opinion=second).split())
        assert 'the value the user named' in prompt
        assert 'never a' in prompt.split('the value the user named')[1]


def test_the_schema_cannot_express_what_only_code_produces(catalog):
    schema = json.dumps(proposed_edits_schema(catalog))
    assert 'disputed' not in schema
    assert 'unreadable' not in schema
    assert user_text(SAID, catalog, second_opinion=False).startswith('| ID | Step |')
    assert (
        PROMPT_VERSION == 'v8'
    )  # bumped with the rules: cached answers are asked again


# --- agreement -----------------------------------------------------------------


def test_an_edit_both_passes_make_is_proposed(catalog):
    proposal = ask(catalog, answer(edit()))
    assert proposal == ProposedEdits.model_validate(
        {'edits': [edit()], 'unresolved': []}
    )


def test_a_different_value_is_disputed_with_both_readings(catalog):
    proposal = ask(catalog, answer(edit(new='4000')), answer(edit(new='4100')))
    assert proposal.edits == []
    item = only(proposal, Disputed)
    assert [[r.new for r in side] for side in (item.first, item.second)] == [
        ['4000'],
        ['4100'],
    ]
    assert item.said == SAID


def test_a_different_column_is_disputed(catalog):
    proposal = ask(
        catalog, answer(edit(target=SPEED)), answer(edit(target=ANNEAL_TIME))
    )
    item = only(proposal, Disputed)
    assert [r.target for r in item.edits()] == [SPEED, ANNEAL_TIME]


def test_an_edit_only_one_pass_makes_is_disputed_with_one_reading(catalog):
    second = edit(target=SOLVENT_2, old='IPA', new='DMSO', said='solvent 2 DMSO')
    for a, b in [
        (answer(edit(), second), answer(edit())),
        (answer(edit()), answer(edit(), second)),
    ]:
        proposal = ask(catalog, a, b)
        assert [e.target for e in proposal.edits] == [SPEED]
        item = only(proposal, Disputed)
        assert len(item.edits()) == 1
        assert [] in (item.first, item.second)  # the other pass: no edit
        assert item.said == 'solvent 2 DMSO'


def test_several_edits_of_the_same_words_make_one_disputed_item(catalog):
    # pass A: speed 4000 and time 4000; pass B: time 4100 and speed 4100
    a = answer(edit(target=SPEED, new='4000'), edit(target=ANNEAL_TIME, new='4000'))
    b = answer(edit(target=ANNEAL_TIME, new='4100'), edit(target=SPEED, new='4100'))
    proposal = ask(catalog, a, b)
    assert proposal.edits == []
    item = only(proposal, Disputed)
    assert [(r.target, r.new) for r in item.edits()] == [
        (SPEED, '4000'),
        (ANNEAL_TIME, '4000'),
        (ANNEAL_TIME, '4100'),
        (SPEED, '4100'),
    ]


def test_an_agreed_edit_a_pass_also_hedged_on_is_disputed(catalog):
    hedge = {'reason': 'ambiguous', 'candidates': [SPEED, ANNEAL_TIME], 'said': SAID}
    proposal = ask(catalog, answer(edit()), answer(edit(), unresolved=[hedge]))
    assert proposal.edits == []
    # one item: the hedge is pass B's reading of the same words
    item = only(proposal, Disputed)
    assert [type(t) for t in item.first] == [Edit]
    assert [type(t) for t in item.second] == [Edit, Ambiguous]


def test_what_is_no_disagreement(catalog):
    # spacing, case and punctuation of the quote; `old`; a repeat in one pass
    a = answer(
        edit(said='Speed  4000 for samples 1 to 3.', old='3000'),
        edit(said=SAID, old='2999'),
    )
    b = answer(edit(said=SAID, old=None))
    proposal = ask(catalog, a, b)
    assert len(proposal.edits) == 1
    assert proposal.edits[0].old == '3000'  # pass A's first edit is the one kept
    assert proposal.unresolved == []
    # the value's spacing
    assert (
        len(ask(catalog, answer(edit(new=' 4000')), answer(edit(new='4000'))).edits)
        == 1
    )


def test_the_value_is_compared_as_it_would_be_written(catalog):
    def time(new, unit=None):
        return answer(
            edit(target=TIME_2, old='600', new=new, unit=unit, said='time 2 is 2 min')
        )

    # the column's unit or none: 200 either way
    assert len(ask(catalog, time('200'), time('200', 'second')).edits) == 1
    # converted: 2 min and 120 s both write 120
    assert len(ask(catalog, time('2', 'min'), time('120', 's')).edits) == 1
    assert len(ask(catalog, time('120'), time('2', 'min')).edits) == 1
    # 2 min is not 2 s
    assert isinstance(ask(catalog, time('2'), time('2', 'min')).unresolved[0], Disputed)


def test_numbers_agree_by_amount_and_empty_only_with_empty(catalog):
    assert (
        len(ask(catalog, answer(edit(new='4000')), answer(edit(new='4000.0'))).edits)
        == 1
    )
    assert (
        len(ask(catalog, answer(edit(new='4000')), answer(edit(new='4,000'))).edits)
        == 1
    )
    cleared = answer(edit(new=''))
    assert isinstance(ask(catalog, cleared, answer(edit())).unresolved[0], Disputed)


@pytest.mark.parametrize(
    ('said', 'new', 'unit', 'shown'),
    [
        ('uv ozone clean, time1 is 200 second', '200', 'second', '600 → 200, '),
        ('uv ozone clean, time1 is 200', '200', None, '600 → 200, '),
        ('uv ozone clean, time1 is 2 min', '2', 'min', '600 → 2 min (120 s), '),
    ],
)
def test_the_preview_shows_a_conversion_only(synthetic_xlsx, said, new, unit, shown):
    time = edit(target='C011', old='600', new=new, unit=unit, said=said)

    def passes(text, catalog):
        return resolve(text, catalog, FakeCall(answer(time)))

    result = interpret_instruction(said, synthetic_xlsx, passes)
    (written,) = result.ready.values()
    assert shown in result.preview, result.preview
    assert written.value == (120 if unit == 'min' else 200)


def test_text_agrees_ignoring_case(catalog):
    def solvent(new):
        return answer(edit(target=SOLVENT_2, old='IPA', new=new, said='solvent 2 dmso'))

    # a term the sheet does not hold: pass A's spelling is written
    (agreed,) = ask(catalog, solvent('dmso'), solvent('DMSO')).edits
    assert agreed.new == 'dmso'
    # another word is still a disagreement
    only(ask(catalog, solvent('dmso'), solvent('DMF')), Disputed)


def test_text_the_sheet_holds_is_written_as_the_sheet_spells_it(catalog):
    # "Acetone" is in Solvent 1; the passes said it in two other cases
    a = answer(
        edit(target=SOLVENT_2, old='IPA', new='acetone', said='solvent 2 acetone')
    )
    b = answer(
        edit(target=SOLVENT_2, old='IPA', new='ACETONE', said='solvent 2 acetone')
    )
    (agreed,) = ask(catalog, a, b).edits
    assert agreed.new == 'Acetone'


def test_the_spelling_comes_from_the_same_field_of_the_numbered_siblings(catalog):
    # "Material name 2" holds "PbI2": "Material name 1" is the same field
    material = edit(target='C045', old=None, new='pbi2', said='material 1 pbi2')
    assert read_answer(answer(material), SAID, catalog).edits[0].new == 'PbI2'
    # "Solute 1 type" is another field than a name of the same solute
    columns = {c.id: c for c in catalog.columns}
    assert _field(columns['C022']) == 'solute type'
    assert _field(columns['C045']) == _field(columns['C048']) == 'material name'
    assert _field(columns['C010']) == _field(columns['C013']) == 'solvent'


def test_a_case_only_correction_and_other_steps_keep_the_words(catalog):
    # Solvent 1 holds "Acetone" on every sample: writing "ACETONE" there is
    # the edit, not a spelling to copy
    capitals = edit(
        target=CLEAN_SOLVENT_1, old='Acetone', new='ACETONE', said='solvent 1 ACETONE'
    )
    assert read_answer(answer(capitals), SAID, catalog).edits[0].new == 'ACETONE'
    # on some samples only, the others still "Acetone": still the correction
    one = {**capitals, 'rows': {'samples': ['1'], 'subbatch': None}}
    assert read_answer(answer(one), SAID, catalog).edits[0].new == 'ACETONE'
    # a column of another step is no sibling: its spelling is not taken
    spin = edit(target=SPEED, new='acetone', said='speed acetone')
    assert read_answer(answer(spin), SAID, catalog).edits[0].new == 'acetone'
    # a number is left as said
    assert read_answer(answer(edit(new='4000')), SAID, catalog).edits[0].new == '4000'


def test_a_range_and_the_same_samples_listed_agree(catalog):
    a = answer(edit(rows={'first': '1', 'last': '3', 'subbatch': None}))
    b = answer(edit(rows={'samples': ['3', '1', '2'], 'subbatch': None}))
    proposal = ask(catalog, a, b)
    assert len(proposal.edits) == 1
    assert isinstance(proposal.edits[0].rows, Range)
    # a range that cannot be followed ("samples 3 to 1") stays what it is
    a = answer(edit(rows={'first': '3', 'last': '1', 'subbatch': None}))
    b = answer(edit(rows={'samples': ['1', '2', '3'], 'subbatch': None}))
    assert isinstance(ask(catalog, a, b).unresolved[0], Disputed)
    # a label the sheet does not have is outside the schema altogether
    a = answer(edit(rows={'first': '1', 'last': '99', 'subbatch': None}))
    assert isinstance(ask(catalog, a).unresolved[0], Unreadable)


def test_every_sample_named_as_a_range_agrees_with_all(catalog):
    # the synthetic sheet has samples 1-5 and 7-13: "1 to 13" is all of them
    every = answer(edit(rows={'first': '1', 'last': '13', 'subbatch': None}))
    proposal = ask(catalog, every, answer(edit(rows='all')))
    assert len(proposal.edits) == 1
    assert proposal.unresolved == []
    everyone = [s.label for s in catalog.samples]
    listed = answer(edit(rows={'samples': everyone, 'subbatch': None}))
    assert len(ask(catalog, answer(edit()), listed).edits) == 1
    # either order; the named samples are the ones kept
    proposal = ask(catalog, answer(edit(rows='all')), every)
    assert len(proposal.edits) == 1
    assert isinstance(proposal.edits[0].rows, Range)
    # fewer samples than all is still a disagreement
    some = answer(edit(rows={'first': '1', 'last': '5', 'subbatch': None}))
    assert isinstance(ask(catalog, some, answer(edit())).unresolved[0], Disputed)


def test_rows_are_the_sheets_rows_with_subbatches(two_subbatches):
    one = next(s.subbatch for s in two_subbatches.samples if s.row == 9)  # noqa: PLR2004
    zero = next(s.subbatch for s in two_subbatches.samples if s.row == 3)  # noqa: PLR2004
    a = answer(edit(rows={'first': '1', 'last': '3', 'subbatch': one}))
    b = answer(edit(rows={'samples': ['1', '2', '3'], 'subbatch': one}))
    assert len(ask(two_subbatches, a, b).edits) == 1
    c = answer(edit(rows={'samples': ['1', '2', '3'], 'subbatch': zero}))
    assert isinstance(ask(two_subbatches, a, c).unresolved[0], Disputed)


def test_samples_the_sheet_cannot_place_are_compared_as_said(two_subbatches):
    # label 1 is in both subbatches and none is said: the rows cannot be
    # resolved, the words are compared instead, and resolve does not crash
    same = answer(edit(rows={'samples': ['1', '2'], 'subbatch': None}))
    proposal = ask(two_subbatches, same)
    assert len(proposal.edits) == 1
    other = answer(edit(rows={'samples': ['2', '1'], 'subbatch': None}))
    assert isinstance(ask(two_subbatches, same, other).unresolved[0], Disputed)


def test_a_hedge_on_the_other_passs_cut_of_the_quote_counts(catalog):
    a = answer(edit(said='speed 4000'))
    hedge = {'reason': 'ambiguous', 'candidates': [SPEED, ANNEAL_TIME], 'said': SAID}
    b = answer(edit(said=SAID), unresolved=[hedge])
    proposal = ask(catalog, a, b)
    assert proposal.edits == []
    item = only(proposal, Disputed)
    assert isinstance(item.second[-1], Ambiguous)


def test_where_is_its_own_reading(catalog):
    where = {'where': VARIATION, 'equals': '20min'}
    a = answer(edit(rows=where))
    assert (
        len(ask(catalog, a, answer(edit(rows={**where, 'equals': ' 20min'}))).edits)
        == 1
    )
    assert isinstance(
        ask(catalog, a, answer(edit(rows={**where, 'equals': None}))).unresolved[0],
        Disputed,
    )


def test_an_anchor_is_no_disagreement_and_is_kept(catalog, synthetic_xlsx):
    # the same cells and value, with and without an anchor: one write; the
    # anchor is kept, whichever pass gave it, so ground checks it
    said = 'time of the DMSO 300'
    time = {
        'target': TIME_2,
        'said': said,
        'new': '300',
        'old': '600',
        'unit': None,
        'said_old': None,
    }
    dmso = edit(**time, anchor={'column': SOLVENT_2, 'equals': 'DMSO'})
    plain = edit(**time, anchor=None)
    for a, b in [([dmso], [plain]), ([plain], [dmso]), ([plain, dmso], [plain])]:
        proposal = ask(catalog, answer(*a), answer(*b))
        assert (proposal.edits, proposal.unresolved) == (
            [Edit.model_validate(dmso)],
            [],
        )
        # solvent 2 holds IPA, not DMSO: a check on the edit, in either order
        result = interpret_instruction(
            said, synthetic_xlsx, lambda t, c: ask(c, answer(*a), answer(*b), t)
        )
        (ready,) = result.ready.values()
        assert "not 'DMSO'" in ready.checks[0]
    # two different anchors for the same write: each is a reading
    other = edit(**time, anchor={'column': SOLVENT_2, 'equals': 'IPA'})
    item = only(ask(catalog, answer(dmso), answer(other)), Disputed)
    assert [r.anchor.equals for r in item.edits()] == ['DMSO', 'IPA']
    # a different value is still a disagreement, anchor or not
    item = only(ask(catalog, answer(dmso), answer({**plain, 'new': '400'})), Disputed)
    assert [r.new for r in item.edits()] == ['300', '400']


def test_an_old_value_said_is_no_disagreement_and_is_kept(catalog, synthetic_xlsx):
    said = 'step 2 rotation speed from 2000 to 4000'
    heard = edit(said=said, said_old='2000')
    missed = edit(said=said)
    for a, b in [(heard, missed), (missed, heard)]:
        proposal = ask(catalog, answer(a), answer(b))
        assert [e.said_old for e in proposal.edits] == ['2000']
        # the sheet holds 3000: a check, whichever pass heard it
        result = interpret_instruction(
            said, synthetic_xlsx, lambda t, c: ask(c, answer(a), answer(b), t)
        )
        (ready,) = result.ready.values()
        assert "you said it was '2000'" in ready.checks[0]
        # the 2000 is used, by said_old: no "no update in" for it
        assert 'no update in' not in result.preview
    # one amount written two ways is one old value
    for same in ('2,000', '2000.0'):
        proposal = ask(catalog, answer(heard), answer(edit(said=said, said_old=same)))
        assert [e.said_old for e in proposal.edits] == ['2000'], same
    # one number in two units is two old values
    minutes = edit(said=said, said_old='2 min')
    item = only(
        ask(catalog, answer(minutes), answer(edit(said=said, said_old='2 h'))),
        Disputed,
    )
    assert [r.said_old for r in item.edits()] == ['2 min', '2 h']
    # two old values heard: each is a reading
    other = edit(said=said, said_old='3000')
    item = only(ask(catalog, answer(heard), answer(other)), Disputed)
    assert [r.said_old for r in item.edits()] == ['2000', '3000']
    # the readings differ only there: the preview says which is which
    result = interpret_instruction(
        said,
        synthetic_xlsx,
        lambda t, c: ask(c, answer(heard), answer(other), t),
    )
    assert 'you said it was 2000' in result.preview
    assert 'you said it was 3000' in result.preview


def test_repeats_of_one_write_in_a_pass_are_all_compared(catalog):
    said = 'time of the DMSO 300'
    time = {
        'target': TIME_2,
        'said': said,
        'new': '300',
        'old': '600',
        'unit': None,
        'said_old': None,
    }
    dmso = edit(**time, anchor={'column': SOLVENT_2, 'equals': 'DMSO'})
    ipa = edit(**time, anchor={'column': SOLVENT_2, 'equals': 'IPA'})
    # only one pass, through two anchors: both are shown
    item = only(ask(catalog, answer(dmso, ipa), answer()), Disputed)
    assert [r.anchor.equals for r in item.edits()] == ['DMSO', 'IPA']
    # a hedge on the quote of a later repeat counts too
    repeat = {**time, 'said': 'time 300', 'anchor': None}
    hedge = {'reason': 'not_found', 'said': 'time 300'}
    proposal = ask(
        catalog,
        answer(edit(**{**time, 'anchor': None}), edit(**repeat)),
        answer(edit(**{**time, 'anchor': None}), unresolved=[hedge]),
    )
    assert proposal.edits == []
    item = only(proposal, Disputed)
    assert isinstance(item.second[-1], NotFound)


def test_unresolved_items_of_either_pass_are_kept_once(catalog):
    ambiguous = {
        'reason': 'ambiguous',
        'candidates': [SPEED, ANNEAL_TIME],
        'said': 'time 20',
    }
    reversed_ = {**ambiguous, 'candidates': [ANNEAL_TIME, SPEED]}
    not_found = {'reason': 'not_found', 'said': 'anneal'}
    a = answer(edit(), unresolved=[ambiguous, not_found])
    b = answer(edit(), unresolved=[reversed_])
    proposal = ask(catalog, a, b)
    assert len(proposal.edits) == 1
    assert [type(u) for u in proposal.unresolved] == [Ambiguous, NotFound]


def test_ambiguous_on_the_same_words_is_one_item_with_every_candidate(catalog):
    # pass A names three columns, pass B two of them and one more
    said = 'rotation speed 2500'
    a = {
        'reason': 'ambiguous',
        'candidates': [SPEED, ANNEAL_TIME, TIME_2],
        'said': said,
    }
    b = {'reason': 'ambiguous', 'candidates': [SPEED, SOLVENT_2], 'said': said}
    item = only(ask(catalog, answer(unresolved=[a]), answer(unresolved=[b])), Ambiguous)
    assert item.candidates == [SPEED, ANNEAL_TIME, TIME_2, SOLVENT_2]


def test_ambiguous_on_words_within_other_words_is_one_item(catalog):
    longer = 'step 2 spin coating, rotation speed 1000 for all samples'
    a = {'reason': 'ambiguous', 'candidates': [SPEED], 'said': longer}
    b = {
        'reason': 'ambiguous',
        'candidates': [ANNEAL_TIME, SPEED],
        'said': 'rotation speed 1000 for all samples',
    }
    item = only(ask(catalog, answer(unresolved=[a]), answer(unresolved=[b])), Ambiguous)
    assert (item.said, item.candidates) == (longer, [SPEED, ANNEAL_TIME])
    # on other words, two items
    c = {**b, 'said': 'time 20'}
    apart = ask(catalog, answer(unresolved=[a]), answer(unresolved=[c]))
    assert [u.said for u in apart.unresolved] == [longer, 'time 20']


def test_a_broad_ambiguous_quote_over_two_clauses_is_not_merged(catalog):
    broad = {
        'reason': 'ambiguous',
        'candidates': [SPEED, ANNEAL_TIME],
        'said': 'speed 1000 and time 20',
    }
    speed = {'reason': 'ambiguous', 'candidates': [SPEED, TIME_2], 'said': 'speed 1000'}
    time = {'reason': 'ambiguous', 'candidates': [ANNEAL_TIME], 'said': 'time 20'}
    proposal = ask(
        catalog, answer(unresolved=[broad]), answer(unresolved=[speed, time])
    )
    assert [(u.said, u.candidates) for u in proposal.unresolved] == [
        ('speed 1000 and time 20', [SPEED, ANNEAL_TIME]),
        ('speed 1000', [SPEED, TIME_2]),
        ('time 20', [ANNEAL_TIME]),
    ]


def test_an_anchor_on_the_written_column_by_the_old_value_is_dropped(catalog):
    # "solvent 2 is DMSO", pointed through Solvent 2 = IPA: the table's old value
    said = 'solvent 2 is DMSO for samples 1 to 3'
    pointed = edit(
        target=SOLVENT_2,
        old='IPA',
        new='DMSO',
        said=said,
        anchor={'column': SOLVENT_2, 'equals': 'IPA'},
    )
    assert ask(catalog, answer(pointed)).edits[0].anchor is None
    # said by the user, it picks the cells: kept
    named = {**pointed, 'said': 'the IPA of samples 1 to 3 to DMSO'}
    assert ask(catalog, answer(named)).edits[0].anchor is not None
    # rows of mixed values: the anchor's check is the only one that tells
    # rows outside it are written, kept ("the hot ones" read as 120)
    mixed = edit(
        target='C055',  # substrate temperature: 120 on samples 1–5, 7; 115 on 8–13
        old=None,
        new='118',
        said='the hot ones to 118',
        anchor={'column': 'C055', 'equals': '120'},
    )
    assert ask(catalog, answer(mixed)).edits[0].anchor is not None
    # an anchor that is not `old`: kept
    other = {**pointed, 'anchor': {'column': SOLVENT_2, 'equals': 'Ethanol'}}
    assert ask(catalog, answer(other)).edits[0].anchor is not None


# --- answers that cannot be read ---------------------------------------------------


@pytest.mark.parametrize(
    ('text', 'detail'),
    [
        ('not json', 'Expecting value'),
        (None, 'the answer is not text'),
        ({'edits': []}, 'the answer is not text'),
        ('[]', 'validation error'),
        (answer(edit(target='C999')), 'validation error'),
        (answer({**edit(), 'extra': 1}), 'validation error'),
        (answer(edit(rows={'samples': [], 'subbatch': None})), 'validation error'),
        (json.dumps({'edits': []}), 'validation error'),
        (
            '{"edits": [], "unresolved": [{"reason": "disputed", "first": [], "second": [], "said": "x"}]}',
            'validation error',
        ),
    ],
)
def test_an_answer_that_is_not_the_schema_resolves_nothing(catalog, text, detail):
    proposal = ask(catalog, answer(edit()), text)
    assert proposal.edits == []
    item = only(proposal, Unreadable)
    assert item.said == SAID
    assert detail in item.detail
    assert '\n' not in item.detail


def test_a_formula_column_cannot_be_a_target(catalog):
    nomad_id = next(c.id for c in catalog.columns if c.formula)
    assert isinstance(
        ask(catalog, answer(edit(target=nomad_id))).unresolved[0], Unreadable
    )


def test_a_call_that_raises_is_not_caught(catalog):
    def call(system, text, schema):
        raise ConnectionError('no worker')

    with pytest.raises(ConnectionError):
        resolve(SAID, catalog, call)


# --- how the user sees it --------------------------------------------------------


def test_preview_of_disputed_and_unreadable_items(catalog):
    proposal = ask(
        catalog, answer(edit(target=SPEED)), answer(edit(target=ANNEAL_TIME))
    )
    assert preview([], proposal.unresolved, NOTHING, catalog) == (
        f'disputed: "{SAID}"\n'
        '   - Reading 1: 2: Spin Coating · Rotation speed [rpm] → 4000\n'
        '   - Reading 2: 2: Spin Coating · Annealing time [min] → 4000'
    )
    one = edit(
        target=SOLVENT_2,
        rows={'first': '1', 'last': '3', 'subbatch': None},
        old='IPA',
        new='',
        said='clear solvent 2 of 1 to 3',
    )
    proposal = ask(catalog, answer(edit(), one), answer(edit()))
    assert preview([], proposal.unresolved, NOTHING, catalog) == (
        'disputed: "clear solvent 2 of 1 to 3"\n'
        '   - Reading 1: 1: Cleaning UV-Ozone · Solvent 2 → (empty)\n'
        '   - Reading 2: no edit'
    )
    proposal = ask(catalog, 'not json')
    assert preview([], proposal.unresolved, NOTHING, catalog) == (
        '? sand could not propose an edit for this note. '
        'Try again, or write the note more clear.'
    )


def test_a_hedge_on_words_around_an_edit_is_that_passs_reading(catalog):
    # pass A: the speed; pass B: the longer words could be either column
    said = 'speed 4000 for samples 1 to 3'
    longer = f'time 1 is 1 for 3. {said}'
    hedge = {'reason': 'ambiguous', 'candidates': [ANNEAL_TIME, SPEED], 'said': longer}
    proposal = ask(catalog, answer(edit(said=said)), answer(unresolved=[hedge]))
    assert preview([], proposal.unresolved, NOTHING, catalog) == (
        f'disputed: "{longer}"\n'
        '   - Reading 1: 2: Spin Coating · Rotation speed [rpm] → 4000\n'
        '   - Reading 2:\n'
        '       - 2: Spin Coating · Annealing time [min]\n'
        '       - 2: Spin Coating · Rotation speed [rpm]'
    )
    # one choice is still a list: under its label
    one = {**hedge, 'candidates': [ANNEAL_TIME]}
    proposal = ask(catalog, answer(edit(said=said)), answer(unresolved=[one]))
    assert preview([], proposal.unresolved, NOTHING, catalog) == (
        f'disputed: "{longer}"\n'
        '   - Reading 1: 2: Spin Coating · Rotation speed [rpm] → 4000\n'
        '   - Reading 2:\n'
        '       - 2: Spin Coating · Annealing time [min]'
    )


def test_a_reading_shows_subbatch_anchor_and_an_empty_where(two_subbatches):
    one = next(s.subbatch for s in two_subbatches.samples if s.row == 9)  # noqa: PLR2004
    a = edit(
        target=TIME_2,
        rows={'samples': ['1', '2'], 'subbatch': one},
        anchor={'column': SOLVENT_2, 'equals': 'DMSO'},
        old='600',
        new='300',
        said='time of the DMSO 300 for 1 and 2 of subbatch 1',
    )
    b = {**a, 'rows': {'where': VARIATION, 'equals': None}, 'anchor': None}
    proposal = ask(two_subbatches, answer(a), answer(b))
    assert preview([], proposal.unresolved, NOTHING, two_subbatches).splitlines()[
        1:
    ] == [
        f'   - Reading 1: 1: Cleaning UV-Ozone · Time 2 [s] → 300, samples 1, 2 of '
        f'subbatch {one}, pointed through Solvent 2 = DMSO',
        '   - Reading 2: 1: Cleaning UV-Ozone · Time 2 [s] → 300, where Experiment '
        'Info · Variation is empty (no sample)',
    ]


def test_a_reading_by_all_or_by_a_value_names_its_samples(catalog):
    # "the spin coating which uses ...": every sample, or the rows holding
    # it; the samples tell the readings apart
    rows = {'where': SUBSTRATE_TEMP, 'equals': '115'}
    proposal = ask(catalog, answer(edit()), answer(edit(rows=rows)))
    lines = preview([], proposal.unresolved, NOTHING, catalog).splitlines()
    assert lines[1].endswith('→ 4000, all samples (1–5, 7–13)')
    assert lines[2].endswith('Substrate temperature [°C] = 115 (samples 8–13)')


def test_a_value_the_column_cannot_hold_is_refused_not_disputed(synthetic_xlsx):
    # pass A: the column, unsure of the value; pass B: "fast" as the value
    said = 'step 2 spin coating, rotation speed fast for sample 2'
    hedge = {'reason': 'ambiguous', 'candidates': [SPEED], 'said': said}
    fast = edit(rows={'samples': ['2'], 'subbatch': None}, new='fast', said=said)

    def call(text, catalog):
        return resolve(
            text, catalog, FakeCall(answer(unresolved=[hedge]), answer(fast))
        )

    result = interpret_instruction(said, synthetic_xlsx, call)
    assert result.ready == {}
    assert result.preview == (
        f'✗ not applied: "{said}"\n'
        "   Rotation speed [rpm] (2: Spin Coating) holds numbers; 'fast' is not a number"
    )
    # two such parts: each is refused with its reason
    five = f'{said}; later rotation speed fast for sample 5'
    hedges = [hedge, {**hedge, 'said': 'later rotation speed fast for sample 5'}]
    again = {
        **fast,
        'rows': {'samples': ['5'], 'subbatch': None},
        'said': hedges[1]['said'],
    }
    proposal = ask(
        build(synthetic_xlsx),
        answer(unresolved=hedges),
        answer(fast, again),
        instruction=five,
    )
    assert [e.rows.samples for e in proposal.edits] == [['2'], ['5']]
    # a pass with an edit the column can hold: still two readings
    four = edit(rows={'samples': ['2'], 'subbatch': None}, said=said)
    proposal = ask(build(synthetic_xlsx), answer(four), answer(fast), instruction=said)
    only(proposal, Disputed)
    # a pass unsure between that column and a text one, which takes "fast":
    # still two readings
    either = {**hedge, 'candidates': [SPEED, SOLVENT_2]}
    proposal = ask(
        build(synthetic_xlsx),
        answer(unresolved=[either]),
        answer(fast),
        instruction=said,
    )
    only(proposal, Disputed)


def test_interpret_instruction_applies_nothing_disputed_or_unreadable(synthetic_xlsx):
    def differing(text, catalog):
        return resolve(
            text,
            catalog,
            FakeCall(answer(edit(target=SPEED)), answer(edit(target=ANNEAL_TIME))),
        )

    result = interpret_instruction(SAID, synthetic_xlsx, differing)
    assert result.ready == {}
    assert result.preview.startswith(f'disputed: "{SAID}"')
    assert '⚠' not in result.preview  # the quote covers the instruction

    def unreadable(text, catalog):
        return resolve(text, catalog, FakeCall('not json'))

    result = interpret_instruction(SAID, synthetic_xlsx, unreadable)
    assert result.ready == {}
    assert result.preview == (
        '? sand could not propose an edit for this note. '
        'Try again, or write the note more clear.'
    )


# --- the three steps the workflow takes ----------------------------------------


def test_prompts_are_what_resolve_asks(catalog):
    call = FakeCall(answer())
    resolve(SAID, catalog, call)
    asked = prompts(SAID, catalog)
    assert [(p.system, p.text, p.schema) for p in asked] == call.seen
    assert [p.second_opinion for p in asked] == [False, True]


def test_read_answer_then_agree_is_resolve(catalog):
    a, b = answer(edit()), answer(edit(old='3,000'))
    opinions = [read_answer(x, SAID, catalog) for x in (a, b)]
    assert agree(opinions, catalog) == ask(catalog, a, b)
    assert agree([opinions[0], read_answer(None, SAID, catalog)], catalog) == ask(
        catalog, a, None
    )
    assert isinstance(read_answer(None, SAID, catalog), Unreadable)


def test_all_agreed_with_the_samples_said_has_no_contradiction(synthetic_xlsx):
    said = 'speed 4000 for samples 1 to 13'
    every = edit(rows={'first': '1', 'last': '13', 'subbatch': None}, said=said)

    def passes(text, catalog):
        return resolve(
            text, catalog, FakeCall(answer({**every, 'rows': 'all'}), answer(every))
        )

    result = interpret_instruction(said, synthetic_xlsx, passes)
    assert len(result.ready) == 1
    assert 'you named samples' not in result.preview


def test_one_subbatchs_samples_are_not_all(two_subbatches):
    one = next(s.subbatch for s in two_subbatches.samples if s.row == 9)  # noqa: PLR2004
    labels = [s.label for s in two_subbatches.samples if s.subbatch == one]
    a = answer(edit(rows={'samples': labels, 'subbatch': one}))
    assert isinstance(ask(two_subbatches, a, answer(edit())).unresolved[0], Disputed)


def test_all_with_an_anchor_keeps_the_samples_the_other_pass_named(synthetic_xlsx):
    said = 'time of the IPA to 300 for samples 1 to 13'
    every = edit(
        target=TIME_2,
        old='600',
        new='300',
        rows={'first': '1', 'last': '13', 'subbatch': None},
        said=said,
    )
    anchored = {
        **every,
        'rows': 'all',
        'anchor': {'column': SOLVENT_2, 'equals': 'IPA'},
    }

    def passes(text, catalog):
        return resolve(text, catalog, FakeCall(answer(anchored), answer(every)))

    result = interpret_instruction(said, synthetic_xlsx, passes)
    assert len(result.ready) == 1, result.preview
    assert 'you named samples' not in result.preview
    (kept,) = result.ready.values()
    assert kept.edit.anchor is not None  # pass A's anchor, still checked


def test_a_hedge_on_words_around_an_agreed_edit_disputes_it(catalog):
    hedge = {
        'reason': 'ambiguous',
        'candidates': [SPEED, ANNEAL_TIME],
        'said': f'set {SAID} now',
    }
    proposal = ask(catalog, answer(edit()), answer(edit(), unresolved=[hedge]))
    assert proposal.edits == []
    item = only(proposal, Disputed)
    assert [type(t) for t in item.second] == [Edit, Ambiguous]


def test_each_pass_keeps_its_reading_when_pointers_differ(catalog):
    dmso = edit(anchor={'column': SOLVENT_2, 'equals': 'DMSO'})
    ipa = edit(anchor={'column': SOLVENT_2, 'equals': 'IPA'})
    item = only(ask(catalog, answer(dmso, ipa), answer(edit())), Disputed)
    assert [r.anchor.equals for r in item.first] == ['DMSO', 'IPA']
    assert [r.anchor for r in item.second] == [None]


def test_a_where_on_the_subbatch_is_those_samples(two_subbatches):
    subbatch = next(
        c.id for c in two_subbatches.columns if c.column.header == 'Subbatch'
    )
    one = next(s.subbatch for s in two_subbatches.samples if s.row == 9)  # noqa: PLR2004
    listed = edit(rows={'first': '1', 'last': '6', 'subbatch': one})
    where = edit(rows={'where': subbatch, 'equals': one})
    assert len(ask(two_subbatches, answer(listed), answer(where)).edits) == 1


def test_a_where_on_a_value_column_agrees_by_its_samples(two_subbatches):
    info = next(c for c in two_subbatches.columns if c.column.header == 'Subbatch')
    section = dataclasses.replace(info.column.section, position=2)
    moved = dataclasses.replace(
        info, column=dataclasses.replace(info.column, section=section)
    )
    catalog = dataclasses.replace(
        two_subbatches,
        columns=tuple(moved if c is info else c for c in two_subbatches.columns),
    )
    one = next(s.subbatch for s in catalog.samples if s.row == 9)  # noqa: PLR2004
    said = 'speed 4000 for the first six samples'
    listed = edit(rows={'first': '1', 'last': '6', 'subbatch': one}, said=said)
    where = edit(rows={'where': info.id, 'equals': one}, said=said)
    # the same samples on this sheet: agreed; the value of the where is not
    # in the words, the samples named are kept
    (agreed,) = ask(catalog, answer(listed), answer(where)).edits
    assert agreed.rows == Range(first='1', last='6', subbatch=one)


def test_a_where_the_user_said_is_kept_when_both_name_the_same_samples(catalog):
    # Solvent 2 is IPA on every sample: "where solvent 2 is IPA" and "all"
    # write the same cells today; the rule is kept, so that it picks other
    # samples once a note above changes Solvent 2
    said = 'time 2 to 700 for the samples where solvent 2 is IPA'
    where = {'where': SOLVENT_2, 'equals': 'IPA'}
    for a, b in (('all', where), (where, 'all')):
        (agreed,) = ask(
            catalog,
            answer(edit(target=TIME_2, rows=a, old='600', new='700', said=said)),
            answer(edit(target=TIME_2, rows=b, old='600', new='700', said=said)),
            instruction=said,
        ).edits
        assert agreed.rows == Where(where=SOLVENT_2, equals='IPA')


def test_an_empty_where_said_as_empty_is_kept(catalog):
    # Solvent 1 volume of step 2 is empty on every sample
    said = 'time 2 to 700 where the solvent 1 volume of step 2 is empty'
    empty = {'where': 'C021', 'equals': None}
    (agreed,) = ask(
        catalog,
        answer(edit(target=TIME_2, rows='all', old='600', new='700', said=said)),
        answer(edit(target=TIME_2, rows=empty, old='600', new='700', said=said)),
        instruction=said,
    ).edits
    assert agreed.rows == Where(where='C021', equals=None)


def test_two_conditions_on_the_same_samples_today_are_two_readings(catalog):
    # Solvent 1 is Acetone and Solvent 2 is IPA on every sample: the same
    # rows now, other rows once a note changes either
    said = 'time 2 to 700 for the samples where the solvent is IPA'
    one = {'where': 'C010', 'equals': 'Acetone'}
    two = {'where': SOLVENT_2, 'equals': 'IPA'}
    proposal = ask(
        catalog,
        answer(edit(target=TIME_2, rows=one, old='600', new='700', said=said)),
        answer(edit(target=TIME_2, rows=two, old='600', new='700', said=said)),
        instruction=said,
    )
    assert proposal.edits == []
    only(proposal, Disputed)


def test_the_step_a_note_was_made_on_is_given_before_the_instruction(catalog):
    step = catalog.columns[-1].column.section.label
    for prompt in prompts(SAID, catalog, step):
        assert prompt.text.endswith(f'\nWorking on step: {step}\nInstruction: {SAID}')
    for prompt in prompts(SAID, catalog):
        assert 'Working on step' not in prompt.text


def test_a_label_counts_as_a_step_only_when_the_sheet_has_it(catalog):
    step = catalog.columns[-1].column.section.label
    assert known_step(step, catalog) == step
    assert known_step(step.lower(), catalog) == ''
    assert known_step('second try', catalog) == ''
    assert known_step('', catalog) == ''
