import dataclasses
import json

import pytest
from synthetic_sheet import edited, letter

from sand.sheet_edit.apply import interpret_instruction
from sand.sheet_edit.catalog import build
from sand.sheet_edit.coverage import Coverage
from sand.sheet_edit.edits import (
    Ambiguous,
    NotFound,
    ProposedEdits,
    Range,
    Samples,
    Uncertain,
    Unreadable,
    proposed_edits_schema,
)
from sand.sheet_edit.preview import preview
from sand.sheet_edit.render import table
from sand.sheet_edit.resolve import PROMPT_VERSION, resolve, system_prompt, user_text

SPEED, ANNEAL_TIME, SOLVENT_2, VARIATION, TIME_2 = (
    'C025',
    'C027',
    'C013',
    'C007',
    'C014',
)
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
    assert system_a == system_prompt(reverse=False)
    assert system_b == system_prompt(reverse=True)
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


def test_the_schema_cannot_express_what_only_code_produces(catalog):
    schema = json.dumps(proposed_edits_schema(catalog))
    assert 'uncertain' not in schema
    assert 'unreadable' not in schema
    assert user_text(SAID, catalog, reverse=False).startswith('| ID | Step |')
    assert PROMPT_VERSION == 'v1'


# --- agreement -----------------------------------------------------------------


def test_an_edit_both_passes_make_is_proposed(catalog):
    proposal = ask(catalog, answer(edit()))
    assert proposal == ProposedEdits.model_validate(
        {'edits': [edit()], 'unresolved': []}
    )


def test_a_different_value_is_uncertain_with_both_readings(catalog):
    proposal = ask(catalog, answer(edit(new='4000')), answer(edit(new='4100')))
    assert proposal.edits == []
    item = only(proposal, Uncertain)
    assert [r.new for r in item.readings] == ['4000', '4100']
    assert (item.said, item.why) == (
        SAID,
        'the two passes read these words differently',
    )


def test_a_different_column_is_uncertain(catalog):
    proposal = ask(
        catalog, answer(edit(target=SPEED)), answer(edit(target=ANNEAL_TIME))
    )
    item = only(proposal, Uncertain)
    assert [r.target for r in item.readings] == [SPEED, ANNEAL_TIME]


def test_an_edit_only_one_pass_makes_is_uncertain_with_one_reading(catalog):
    second = edit(target=SOLVENT_2, old='IPA', new='DMSO', said='solvent 2 DMSO')
    for a, b in [
        (answer(edit(), second), answer(edit())),
        (answer(edit()), answer(edit(), second)),
    ]:
        proposal = ask(catalog, a, b)
        assert [e.target for e in proposal.edits] == [SPEED]
        item = only(proposal, Uncertain)
        assert len(item.readings) == 1
        assert (item.said, item.why) == (
            'solvent 2 DMSO',
            'only one of the two passes proposed it',
        )


def test_several_edits_of_the_same_words_make_one_uncertain_item(catalog):
    # pass A: speed 4000 and time 4000; pass B: time 4100 and speed 4100
    a = answer(edit(target=SPEED, new='4000'), edit(target=ANNEAL_TIME, new='4000'))
    b = answer(edit(target=ANNEAL_TIME, new='4100'), edit(target=SPEED, new='4100'))
    proposal = ask(catalog, a, b)
    assert proposal.edits == []
    item = only(proposal, Uncertain)
    assert [(r.target, r.new) for r in item.readings] == [
        (SPEED, '4000'),
        (ANNEAL_TIME, '4000'),
        (ANNEAL_TIME, '4100'),
        (SPEED, '4100'),
    ]


def test_an_agreed_edit_a_pass_also_hedged_on_is_uncertain(catalog):
    hedge = {'reason': 'ambiguous', 'candidates': [SPEED, ANNEAL_TIME], 'said': SAID}
    proposal = ask(catalog, answer(edit()), answer(edit(), unresolved=[hedge]))
    assert proposal.edits == []
    uncertain, ambiguous = proposal.unresolved
    assert isinstance(uncertain, Uncertain)
    assert uncertain.why == 'a pass also left these words unresolved'
    assert isinstance(ambiguous, Ambiguous)


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


def test_the_value_is_compared_as_written(catalog):
    a = answer(edit(target=SOLVENT_2, old='IPA', new='dmso', said='solvent 2 dmso'))
    b = answer(edit(target=SOLVENT_2, old='IPA', new='DMSO', said='solvent 2 dmso'))
    item = only(ask(catalog, a, b), Uncertain)
    assert [r.new for r in item.readings] == ['dmso', 'DMSO']


def test_a_range_and_the_same_samples_listed_agree(catalog):
    a = answer(edit(rows={'first': '1', 'last': '3', 'subbatch': None}))
    b = answer(edit(rows={'samples': ['3', '1', '2'], 'subbatch': None}))
    proposal = ask(catalog, a, b)
    assert len(proposal.edits) == 1
    assert isinstance(proposal.edits[0].rows, Range)
    # a range that cannot be followed ("samples 3 to 1") stays what it is
    a = answer(edit(rows={'first': '3', 'last': '1', 'subbatch': None}))
    b = answer(edit(rows={'samples': ['1', '2', '3'], 'subbatch': None}))
    assert isinstance(ask(catalog, a, b).unresolved[0], Uncertain)
    # a label the sheet does not have is outside the schema altogether
    a = answer(edit(rows={'first': '1', 'last': '99', 'subbatch': None}))
    assert isinstance(ask(catalog, a).unresolved[0], Unreadable)


def test_rows_are_the_sheets_rows_with_subbatches(two_subbatches):
    one = next(s.subbatch for s in two_subbatches.samples if s.row == 9)  # noqa: PLR2004
    zero = next(s.subbatch for s in two_subbatches.samples if s.row == 3)  # noqa: PLR2004
    a = answer(edit(rows={'first': '1', 'last': '3', 'subbatch': one}))
    b = answer(edit(rows={'samples': ['1', '2', '3'], 'subbatch': one}))
    assert len(ask(two_subbatches, a, b).edits) == 1
    c = answer(edit(rows={'samples': ['1', '2', '3'], 'subbatch': zero}))
    assert isinstance(ask(two_subbatches, a, c).unresolved[0], Uncertain)


def test_where_anchor_and_all_are_their_own_readings(catalog):
    everyone = [s.label for s in catalog.samples]
    b = answer(edit(rows={'samples': everyone, 'subbatch': None}))
    item = only(ask(catalog, answer(edit()), b), Uncertain)
    assert isinstance(item.readings[1].rows, Samples)
    where = {'where': VARIATION, 'equals': '20min'}
    a = answer(edit(rows=where))
    assert (
        len(ask(catalog, a, answer(edit(rows={**where, 'equals': ' 20min'}))).edits)
        == 1
    )
    assert isinstance(
        ask(catalog, a, answer(edit(rows={**where, 'equals': None}))).unresolved[0],
        Uncertain,
    )
    anchor = {'column': SOLVENT_2, 'equals': 'DMSO'}
    a = answer(
        edit(
            target=TIME_2,
            anchor=anchor,
            said='time of the DMSO 300',
            new='300',
            old='600',
        )
    )
    b = answer(
        edit(
            target=TIME_2,
            anchor=None,
            said='time of the DMSO 300',
            new='300',
            old='600',
        )
    )
    assert isinstance(ask(catalog, a, b).unresolved[0], Uncertain)
    assert len(ask(catalog, a, a).edits) == 1


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
            '{"edits": [], "unresolved": [{"reason": "uncertain", "readings": [], "why": "", "said": "x"}]}',
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


def test_preview_of_uncertain_and_unreadable_items(catalog):
    proposal = ask(
        catalog, answer(edit(target=SPEED)), answer(edit(target=ANNEAL_TIME))
    )
    assert preview([], proposal.unresolved, NOTHING, catalog) == (
        f'? uncertain: "{SAID}"\n'
        '   2: Spin Coating · Rotation speed [rpm] → 4000, all samples or '
        '2: Spin Coating · Annealing time [min] → 4000, all samples\n'
        '   the two passes read these words differently'
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
        '? uncertain: "clear solvent 2 of 1 to 3"\n'
        '   1: Cleaning UV-Ozone · Solvent 2 → (empty), samples 1–3\n'
        '   only one of the two passes proposed it'
    )
    proposal = ask(catalog, 'not json')
    assert preview([], proposal.unresolved, NOTHING, catalog) == (
        "? not done, the model's answer could not be read: "
        'Expecting value: line 1 column 1 (char 0)'
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
        1
    ] == (
        f'   1: Cleaning UV-Ozone · Time 2 [s] → 300, samples 1, 2 of subbatch {one}, '
        'pointed through Solvent 2 = DMSO or '
        '1: Cleaning UV-Ozone · Time 2 [s] → 300, where Experiment Info · Variation is empty'
    )


def test_interpret_instruction_applies_nothing_uncertain_or_unreadable(synthetic_xlsx):
    def differing(text, catalog):
        return resolve(
            text,
            catalog,
            FakeCall(answer(edit(target=SPEED)), answer(edit(target=ANNEAL_TIME))),
        )

    result = interpret_instruction(SAID, synthetic_xlsx, differing)
    assert result.ready == {}
    assert result.preview.startswith(f'? uncertain: "{SAID}"')
    assert '⚠' not in result.preview  # the quote covers the instruction

    def unreadable(text, catalog):
        return resolve(text, catalog, FakeCall('not json'))

    result = interpret_instruction(SAID, synthetic_xlsx, unreadable)
    assert result.ready == {}
    assert result.preview == (
        "? not done, the model's answer could not be read: "
        'Expecting value: line 1 column 1 (char 0)'
    )
