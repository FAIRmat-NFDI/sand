import json

import pytest

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
from sand.sheet_edit.resolve import PROMPT_VERSION, resolve, system_prompt, user_text

SPEED, ANNEAL_TIME, SOLVENT_2, VARIATION = 'C025', 'C027', 'C013', 'C007'
SAID = 'speed 4000 for samples 1 to 3'


@pytest.fixture(scope='module')
def catalog(synthetic_xlsx):
    return build(synthetic_xlsx)


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


class FakeCall:
    """Pass A gets `a`, pass B gets `b` (or the same); records what it saw."""

    def __init__(self, a: str, b: str | None = None):
        self.answers = [a, a if b is None else b]
        self.seen: list[tuple[str, str, dict]] = []

    def __call__(self, system: str, text: str, schema: dict) -> str:
        self.seen.append((system, text, schema))
        return self.answers[len(self.seen) - 1]


def ask(catalog, a, b=None, instruction=SAID):
    return resolve(instruction, catalog, FakeCall(a, b))


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
    (item,) = proposal.unresolved
    assert isinstance(item, Uncertain)
    assert [r.new for r in item.readings] == ['4000', '4100']
    assert item.said == SAID


def test_a_different_column_is_uncertain(catalog):
    proposal = ask(
        catalog, answer(edit(target=SPEED)), answer(edit(target=ANNEAL_TIME))
    )
    (item,) = proposal.unresolved
    assert isinstance(item, Uncertain)
    assert [r.target for r in item.readings] == [SPEED, ANNEAL_TIME]


def test_an_edit_only_one_pass_makes_is_uncertain_with_one_reading(catalog):
    second = edit(target=SOLVENT_2, old='IPA', new='DMSO', said='solvent 2 DMSO')
    proposal = ask(catalog, answer(edit(), second), answer(edit()))
    assert [e.target for e in proposal.edits] == [SPEED]
    (item,) = proposal.unresolved
    assert isinstance(item, Uncertain)
    assert len(item.readings) == 1
    assert item.said == 'solvent 2 DMSO'
    # the other way round too
    proposal = ask(catalog, answer(edit()), answer(edit(), second))
    assert [e.target for e in proposal.edits] == [SPEED]
    assert isinstance(proposal.unresolved[0], Uncertain)


def test_spacing_case_and_old_are_no_disagreement(catalog):
    a = answer(edit(said='Speed  4000 for samples 1 to 3', old='3000'))
    b = answer(edit(said='speed 4000 for samples 1 to 3', old=None))
    proposal = ask(catalog, a, b)
    assert len(proposal.edits) == 1
    assert proposal.edits[0].old == '3000'  # pass A's edit is the one kept
    assert proposal.unresolved == []


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


def test_rows_all_and_every_sample_listed_are_not_the_same_reading(catalog):
    everyone = [s.label for s in catalog.samples]
    b = answer(edit(rows={'samples': everyone, 'subbatch': None}))
    proposal = ask(catalog, answer(edit()), b)
    assert isinstance(proposal.unresolved[0], Uncertain)
    assert isinstance(proposal.unresolved[0].readings[1].rows, Samples)


def test_unresolved_items_of_either_pass_are_kept_once(catalog):
    ambiguous = {
        'reason': 'ambiguous',
        'candidates': [SPEED, ANNEAL_TIME],
        'said': 'time 20',
    }
    not_found = {'reason': 'not_found', 'said': 'anneal'}
    a = answer(edit(), unresolved=[ambiguous, not_found])
    b = answer(edit(), unresolved=[ambiguous])
    proposal = ask(catalog, a, b)
    assert len(proposal.edits) == 1
    assert [type(u) for u in proposal.unresolved] == [Ambiguous, NotFound]


# --- answers that cannot be read ---------------------------------------------------


@pytest.mark.parametrize(
    ('text', 'detail'),
    [
        ('not json', 'Expecting value'),
        (answer(edit(target='C999')), 'validation error'),
        (answer({**edit(), 'extra': 1}), 'validation error'),
        (answer(edit(rows={'samples': [], 'subbatch': None})), 'validation error'),
        (json.dumps({'edits': []}), 'validation error'),
        (
            '{"edits": [], "unresolved": [{"reason": "uncertain", "readings": [], "said": "x"}]}',
            'validation error',
        ),
    ],
)
def test_an_answer_that_is_not_the_schema_resolves_nothing(catalog, text, detail):
    proposal = ask(catalog, answer(edit()), text)
    assert proposal.edits == []
    (item,) = proposal.unresolved
    assert isinstance(item, Unreadable)
    assert item.said == SAID
    assert detail in item.detail
    assert '\n' not in item.detail


def test_a_formula_column_cannot_be_a_target(catalog):
    nomad_id = next(c.id for c in catalog.columns if c.formula)
    proposal = ask(catalog, answer(edit(target=nomad_id)))
    assert isinstance(proposal.unresolved[0], Unreadable)


# --- how the user sees it --------------------------------------------------------


def test_preview_of_an_uncertain_and_an_unreadable_item(catalog):
    nothing = Coverage((), ())
    proposal = ask(
        catalog, answer(edit(target=SPEED)), answer(edit(target=ANNEAL_TIME))
    )
    assert preview([], proposal.unresolved, nothing, catalog) == (
        f'? uncertain: "{SAID}"\n'
        '   2: Spin Coating · Rotation speed [rpm] → 4000, all samples or '
        '2: Spin Coating · Annealing time [min] → 4000, all samples?'
    )
    one = edit(
        target=SOLVENT_2,
        rows={'first': '1', 'last': '3', 'subbatch': None},
        old='IPA',
        new='DMSO',
        said='solvent 2 DMSO',
    )
    proposal = ask(catalog, answer(edit(), one), answer(edit()))
    assert preview([], proposal.unresolved, nothing, catalog) == (
        '? uncertain: "solvent 2 DMSO"\n'
        '   only one of two readings proposed 1: Cleaning UV-Ozone · Solvent 2 → DMSO, samples 1–3'
    )
    proposal = ask(catalog, 'not json')
    assert preview([], proposal.unresolved, nothing, catalog) == (
        "? not done, the model's answer could not be read: "
        'Expecting value: line 1 column 1 (char 0)'
    )
