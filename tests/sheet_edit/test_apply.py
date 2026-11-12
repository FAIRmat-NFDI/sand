import datetime

from sand.sheet_edit.apply import (
    apply_all,
    change_key,
    interpret_instruction,
    warnings,
)
from sand.sheet_edit.changelog import Confirmation
from sand.sheet_edit.edits import Anchor, Edit, NotFound, ProposedEdits, Range, Samples
from sand.sheet_edit.ground import Refused
from sand.sheet_edit.session import carry_of
from sand.sheet_edit.write import verify

CLEAN_SOLVENT_2, CLEAN_TIME_2, VARIATION, SPEED = 'C013', 'C014', 'C007', 'C025'
FIRST_THREE = Range(first='1', last='3', subbatch=None)
FIRST_FOUR = Range(first='1', last='4', subbatch=None)
THREE_ROWS, ALL_ROWS = 3, 12
SOLVENT_2_COLUMN = 13

SOLVENT = 'solvent 2 to DMSO for samples 1 to 3'
TIME = 'the time of the DMSO for samples 1 to 3 to 300'
SPEED_SAID = 'speed 4000'
VARIATION_SAID = 'variation 20min for samples 1 to 3'
TWICE = 'solvent 2 to DMSO for samples 1 to 3 and to ETH for sample 2'
TWO = 'solvent 2 to DMSO for samples 1 to 3 and speed 4000'


def edit(target, rows, old, new, said, **fields):
    fields = {'anchor': None, **fields}
    return Edit(target=target, rows=rows, old=old, new=new, said=said, **fields)


def proposal(*edits, unresolved=()):
    return ProposedEdits(edits=list(edits), unresolved=list(unresolved))


PROPOSALS = {
    SOLVENT: proposal(edit(CLEAN_SOLVENT_2, FIRST_THREE, 'IPA', 'DMSO', SOLVENT)),
    TIME: proposal(
        edit(
            CLEAN_TIME_2,
            FIRST_THREE,
            '600',
            '300',
            TIME,
            anchor=Anchor(column=CLEAN_SOLVENT_2, equals='DMSO'),
        )
    ),
    SPEED_SAID: proposal(edit(SPEED, 'all', '3000', '4000', SPEED_SAID)),
    'speed 3000': proposal(edit(SPEED, 'all', '3000', '3000', 'speed 3000')),
    'speed 4100': proposal(edit(SPEED, 'all', '3000', '4100', 'speed 4000')),
    'speed 5000': proposal(edit(SPEED, 'all', '3000', '5000', 'speed 5000')),
    # 4100 is not in the words: a check
    'spin speed 4000': proposal(edit(SPEED, 'all', '3000', '4100', 'spin speed 4000')),
    'solvent 2 to ETH for samples 1 to 3': proposal(
        edit(
            CLEAN_SOLVENT_2,
            FIRST_THREE,
            'DMSO',
            'ETH',
            'solvent 2 to ETH for samples 1 to 3',
        )
    ),
    'solvent 2 to DMSO for samples 1 to 4': proposal(
        edit(
            CLEAN_SOLVENT_2,
            FIRST_FOUR,
            'IPA',
            'DMSO',
            'solvent 2 to DMSO for samples 1 to 4',
        )
    ),
    VARIATION_SAID: proposal(
        edit(
            VARIATION,
            Samples(samples=['1', '2', '3'], subbatch=None),
            '10min',
            '20min',
            VARIATION_SAID,
        )
    ),
    'speed 4000 and anneal': proposal(
        edit(SPEED, 'all', '3000', '4000', 'speed 4000'),
        unresolved=[NotFound(reason='not_found', said='anneal')],
    ),
    'speed 4000 and annael': proposal(
        edit(SPEED, 'all', '3000', '4000', 'speed 4000'),
        unresolved=[NotFound(reason='not_found', said='anneal')],
    ),
    TWICE: proposal(
        edit(CLEAN_SOLVENT_2, FIRST_THREE, 'IPA', 'DMSO', SOLVENT),
        edit(
            CLEAN_SOLVENT_2,
            Samples(samples=['2'], subbatch=None),
            'IPA',
            'ETH',
            'to ETH for sample 2',
        ),
    ),
    TWO: proposal(
        edit(CLEAN_SOLVENT_2, FIRST_THREE, 'IPA', 'DMSO', SOLVENT),
        edit(SPEED, 'all', '3000', '4000', 'speed 4000'),
    ),
}


class FakeLLM:
    """Canned proposals; counts the calls, like a bill would."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, text, catalog):
        self.calls.append(text)
        return PROPOSALS[text]


def run(original, inputs, confirmed=(), cache=None, llm=None):
    cache = {} if cache is None else cache
    return apply_all(original, inputs, llm or FakeLLM(), set(confirmed), cache)


def keys(step):
    return set(step.interpretation.ready)


# --- interpret_instruction ------------------------------------------------------


def test_one_instruction_is_interpreted_and_nothing_written(synthetic_xlsx):
    result = interpret_instruction('speed 4000 and anneal', synthetic_xlsx, FakeLLM())
    assert result.preview == (
        '2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, all 12 samples\n'
        '   from "speed 4000"\n'
        '? not found: "anneal"'
    )
    assert len(result.ready) == 1
    (ready,) = result.ready.values()
    assert (ready.rows, ready.value) == (tuple(range(3, 15)), 4000)


def test_a_misquoted_edit_is_refused_and_shown_once(synthetic_xlsx):
    result = interpret_instruction('speed 4100', synthetic_xlsx, FakeLLM())
    (refused,) = result.results
    assert isinstance(refused, Refused)
    assert refused.reasons == ('"speed 4000" is not in your instruction',)
    assert result.preview == (
        '✗ not applied: "speed 4000"\n'
        '   "speed 4000" is not in your instruction\n'
        '⚠ no update in: "speed 4100"'
    )
    assert result.ready == {}


def test_a_misquoted_unresolved_item_keeps_its_line(synthetic_xlsx):
    result = interpret_instruction('speed 4000 and annael', synthetic_xlsx, FakeLLM())
    assert result.preview.endswith('? not found: "anneal"\n⚠ misquoted: "anneal"')


def test_two_edits_of_one_instruction_may_not_write_the_same_cell(synthetic_xlsx):
    result = interpret_instruction(TWICE, synthetic_xlsx, FakeLLM())
    first, second = result.results
    assert isinstance(second, Refused)
    assert second.reasons == (
        'Solvent 2 (1: Cleaning UV-Ozone) on samples 2 is already changed by '
        f'"{SOLVENT}"',
    )
    assert list(result.ready.values()) == [first]


def test_a_noop_is_not_ready(synthetic_xlsx):
    result = interpret_instruction('speed 3000', synthetic_xlsx, FakeLLM())
    assert result.ready == {}
    assert result.preview.startswith('= 2: Spin Coating')


# --- apply_all --------------------------------------------------------------------


def test_nothing_confirmed_writes_nothing(synthetic_xlsx):
    steps = run(synthetic_xlsx, [SOLVENT, TIME])
    assert [s.written for s in steps] == [(), ()]
    assert steps[-1].state == synthetic_xlsx


def test_a_later_input_sees_an_earlier_edit_once_it_is_confirmed(synthetic_xlsx):
    steps = run(synthetic_xlsx, [SOLVENT, TIME])
    # no DMSO yet: the anchor check finds it, the edit waits for its own check
    (ready,) = steps[1].interpretation.ready.values()
    assert "holds 'IPA' on samples 1–3, not 'DMSO'" in ready.checks[0]
    assert '⚠ check:' in steps[1].interpretation.preview

    steps = run(synthetic_xlsx, [SOLVENT, TIME], confirmed=keys(steps[0]))
    assert len(steps[0].written) == THREE_ROWS
    assert steps[1].interpretation.preview.startswith(
        '1: Cleaning UV-Ozone · Time 2 [s]: 600 → 300, samples 1–3'
    )
    assert steps[1].written == ()  # not confirmed yet


def test_each_step_is_verified_and_the_final_file_is_original_plus_changes(
    synthetic_xlsx,
):
    steps = run(synthetic_xlsx, [SOLVENT])
    steps = run(synthetic_xlsx, [SOLVENT], confirmed=keys(steps[0]))
    verify(synthetic_xlsx, steps[0].state, list(steps[0].written))
    assert {(c.row, c.after) for c in steps[0].written} == {
        (3, 'DMSO'),
        (4, 'DMSO'),
        (5, 'DMSO'),
    }


def test_only_the_llm_call_is_cached_and_only_for_the_same_state(synthetic_xlsx):
    llm, cache = FakeLLM(), {}
    first = run(synthetic_xlsx, [SOLVENT, TIME, SPEED_SAID], cache=cache, llm=llm)
    assert llm.calls == [SOLVENT, TIME, SPEED_SAID]
    # the same inputs again: nothing changed, nothing asked
    run(synthetic_xlsx, [SOLVENT, TIME, SPEED_SAID], cache=cache, llm=llm)
    assert llm.calls == [SOLVENT, TIME, SPEED_SAID]
    # input 1 confirmed: inputs 2 and 3 see a new sheet and are asked again
    run(synthetic_xlsx, [SOLVENT, TIME, SPEED_SAID], keys(first[0]), cache, llm)
    assert llm.calls[3:] == [TIME, SPEED_SAID]
    # input 2 reworded: only input 2 is asked; input 3's sheet is the same
    run(synthetic_xlsx, [SOLVENT, 'speed 3000', SPEED_SAID], keys(first[0]), cache, llm)
    assert llm.calls[5:] == ['speed 3000']


def test_cached_only_stops_before_the_first_input_the_model_was_not_asked(
    synthetic_xlsx,
):
    llm, cache = FakeLLM(), {}
    first = run(synthetic_xlsx, [SOLVENT, TIME], cache=cache, llm=llm)
    replayed = apply_all(
        synthetic_xlsx, [SOLVENT, TIME, SPEED_SAID], llm, set(), cache, cached_only=True
    )
    assert [s.text for s in replayed] == [SOLVENT, TIME]
    assert [keys(s) for s in replayed] == [keys(first[0]), keys(first[1])]
    assert llm.calls == [SOLVENT, TIME]  # nothing asked
    # input 1 confirmed: input 2's sheet is new, so the replay ends after 1
    confirmed = keys(first[0])
    written = apply_all(
        synthetic_xlsx, [SOLVENT, TIME], llm, confirmed, cache, cached_only=True
    )
    assert [s.text for s in written] == [SOLVENT]
    assert len(written[0].written) == THREE_ROWS
    assert apply_all(synthetic_xlsx, [], llm, set(), cache, cached_only=True) == []


def test_a_confirmation_holds_only_for_the_identical_edit(synthetic_xlsx):
    confirmed = keys(run(synthetic_xlsx, [SOLVENT])[0])
    # the same words again: still confirmed
    assert len(run(synthetic_xlsx, [SOLVENT], confirmed)[0].written) == THREE_ROWS
    # the transcript fixed to four samples: a different edit, confirm again
    reworded = 'solvent 2 to DMSO for samples 1 to 4'
    steps = run(synthetic_xlsx, [reworded], confirmed)
    assert steps[0].written == ()
    assert len(steps[0].interpretation.ready) == 1


def test_a_confirmation_is_bound_to_the_sheet_the_input_was_given(synthetic_xlsx):
    inputs = [SOLVENT, SPEED_SAID]
    confirmed = keys(run(synthetic_xlsx, inputs)[0])
    # input 2 confirmed on the sheet after input 1
    confirmed |= keys(run(synthetic_xlsx, inputs, confirmed)[1])
    steps = run(synthetic_xlsx, inputs, confirmed)
    assert [len(s.written) for s in steps] == [THREE_ROWS, ALL_ROWS]
    # input 1 deleted: its edit is gone, and input 2 now sees another
    # sheet, so its edit is to confirm again even though it is the same
    (step,) = run(synthetic_xlsx, [SPEED_SAID], confirmed)
    assert step.written == ()
    assert len(step.interpretation.ready) == 1
    assert step.state == synthetic_xlsx


def test_edits_of_one_input_are_confirmed_one_by_one(synthetic_xlsx):
    (step,) = run(synthetic_xlsx, [TWO])
    solvent, speed = step.interpretation.ready
    (step,) = run(synthetic_xlsx, [TWO], {solvent})
    assert {c.column for c in step.written} == {SOLVENT_2_COLUMN}
    assert set(step.interpretation.ready) == {solvent, speed}  # keys unchanged


def test_the_same_words_twice_write_once(synthetic_xlsx):
    # both inputs get the same key: the second then finds nothing to change
    confirmed = keys(run(synthetic_xlsx, [SPEED_SAID, SPEED_SAID])[0])
    steps = run(synthetic_xlsx, [SPEED_SAID, SPEED_SAID], confirmed)
    assert [len(s.written) for s in steps] == [ALL_ROWS, 0]
    assert steps[1].interpretation.preview.startswith('= ')


def test_the_writer_may_still_refuse_a_confirmed_edit(synthetic_xlsx):
    # the synthetic sheet has no stored formula results, so a cell a
    # formula reads cannot be written safely
    confirmed = keys(run(synthetic_xlsx, [VARIATION_SAID])[0])
    (step,) = run(synthetic_xlsx, [VARIATION_SAID], confirmed)
    assert step.written == ()
    assert step.state == synthetic_xlsx
    assert step.refused is not None
    assert 'has no stored result' in step.refused
    # a later input, confirmed on the unchanged sheet, is still written
    confirmed |= keys(run(synthetic_xlsx, [VARIATION_SAID, SOLVENT], confirmed)[1])
    steps = run(synthetic_xlsx, [VARIATION_SAID, SOLVENT], confirmed)
    assert [len(s.written) for s in steps] == [0, THREE_ROWS]


def test_a_marked_edit_never_blocks_a_clean_one_of_the_same_cell(synthetic_xlsx):
    said = 'spin coating 2 speed 4000'
    wrong = edit(SPEED, 'all', '3000', '4100', said)  # 4100: a check
    right = edit(SPEED, 'all', '3000', '4000', said)
    result = interpret_instruction(
        said, synthetic_xlsx, lambda t, c: proposal(wrong, right)
    )
    marked, clean = result.results
    assert isinstance(marked, Refused) and 'already changed' in marked.reasons[0]
    assert list(result.ready.values()) == [clean] and clean.checks == ()


# --- a confirmation carried to the same change on another sheet ------------------


def on_speed_4000(text, catalog):
    """The model's answer once speed 4000 is written: it reads 4000 as old."""
    (edit,) = PROPOSALS[text].edits
    return proposal(edit.model_copy(update={'old': '4000'}))


def confirm_all(steps):
    """What "Confirm all" saves: every ready key, with what it confirmed,
    per input."""
    confirmed, carry = set(), []
    for step in steps:
        own = {}
        for key, edit in step.interpretation.ready.items():
            confirmed.add(key)
            own[change_key(step.text, edit)] = (key, frozenset(warnings(edit)))
        carry.append(own)
    return confirmed, carry


def test_confirm_all_writes_a_chain_in_one_round(synthetic_xlsx):
    # both read on the original, both confirmed: the solvent note's
    # sheet changes once the speed is written, its change does not
    inputs = [SPEED_SAID, SOLVENT]
    confirmed, carry = confirm_all(run(synthetic_xlsx, inputs))

    steps = apply_all(synthetic_xlsx, inputs, FakeLLM(), confirmed, {}, carry=carry)

    assert keys(steps[1]).isdisjoint(confirmed)  # a new key on the new sheet
    assert list(steps[1].carried.values())[0] in confirmed
    assert steps[1].written
    verify(synthetic_xlsx, steps[-1].state, steps[0].written + steps[1].written)


def test_the_same_cell_and_new_value_carries_the_old_value_is_not_compared(
    synthetic_xlsx,
):
    # 3000 -> 5000 confirmed; after speed 4000 is written it is 4000 -> 5000
    inputs = [SPEED_SAID, 'speed 5000']
    confirmed, carry = confirm_all(run(synthetic_xlsx, inputs))
    # note 1 from the cache, note 2 read on the new sheet
    cache = {}
    run(synthetic_xlsx, inputs[:1], cache=cache)

    steps = apply_all(
        synthetic_xlsx, inputs, on_speed_4000, confirmed, cache, carry=carry
    )

    assert steps[1].carried
    values = [c for c in steps[1].written if c.field == 'value']
    assert {c.before for c in values} == {4000}
    assert {c.after for c in values} == {5000}


def test_other_samples_are_not_carried(synthetic_xlsx):
    # samples 1 to 4 on the original; 1 to 3 hold DMSO once the first is
    # written, so only sample 4 is left: another change, asked again
    inputs = [SOLVENT, 'solvent 2 to DMSO for samples 1 to 4']
    confirmed, carry = confirm_all(run(synthetic_xlsx, inputs))

    steps = apply_all(synthetic_xlsx, inputs, FakeLLM(), confirmed, {}, carry=carry)

    assert steps[1].carried == {}
    assert steps[1].written == ()


def test_a_new_word_check_is_not_carried(synthetic_xlsx):
    inputs = [SPEED_SAID, 'spin speed 4000']
    first = run(synthetic_xlsx, inputs)
    confirmed, carry = confirm_all(first)
    (marked,) = first[1].interpretation.ready.values()
    assert marked.checks
    cache = {}
    run(synthetic_xlsx, inputs[:1], cache=cache)
    # confirmed past the same check: carried
    steps = apply_all(
        synthetic_xlsx, inputs, on_speed_4000, confirmed, dict(cache), carry=carry
    )
    assert steps[1].carried
    # confirmed when it had no check: the check is new
    carry = [
        {change: (key, frozenset()) for change, (key, _) in own.items()}
        for own in carry
    ]

    steps = apply_all(
        synthetic_xlsx, inputs, on_speed_4000, confirmed, dict(cache), carry=carry
    )

    assert steps[1].carried == {}


def test_a_carry_needs_its_confirmation(synthetic_xlsx):
    inputs = [SPEED_SAID, SOLVENT]
    confirmed, carry = confirm_all(run(synthetic_xlsx, inputs))
    (solvent_key,) = keys(run(synthetic_xlsx, inputs)[1])

    steps = apply_all(
        synthetic_xlsx, inputs, FakeLLM(), confirmed - {solvent_key}, {}, carry=carry
    )

    assert steps[1].carried == {}


def reads_eth_later():
    """The solvent note's answer: IPA as old on the original, ETH after."""
    calls = []

    def resolve(text, catalog):
        if text != SOLVENT:
            return PROPOSALS[text]
        calls.append(text)
        (edit,) = PROPOSALS[text].edits
        return proposal(
            edit.model_copy(update={'old': 'IPA' if len(calls) == 1 else 'ETH'})
        )

    return resolve


def test_a_confirmation_is_never_carried_to_another_input(synthetic_xlsx):
    # notes 1 and 3 say the same; only note 1 was confirmed. Note 2 puts
    # ETH where note 1 put DMSO, so note 3 would change it back: not
    # without its own confirmation
    inputs = [SOLVENT, 'solvent 2 to ETH for samples 1 to 3', SOLVENT]
    first = run(synthetic_xlsx, inputs)
    confirmations = {}
    for step, entry_id in zip(first[:2], ['n1', 'n2'], strict=True):
        for key, ready in step.interpretation.ready.items():
            confirmations[key] = Confirmation(
                by='tester',
                at=datetime.datetime(2026, 11, 12, tzinfo=datetime.UTC),
                input=entry_id,
                change=change_key(step.text, ready),
            )
    carry = carry_of(confirmations, ['n1', 'n2', 'n3'])

    steps = apply_all(
        synthetic_xlsx, inputs, reads_eth_later(), set(confirmations), {}, carry=carry
    )

    assert steps[1].carried  # note 2 read on note 1's DMSO, same change
    # note 3 would change ETH back, the same change as note 1, no warning
    (ready,) = steps[2].interpretation.ready.values()
    assert change_key(SOLVENT, ready) in carry[0]
    assert not warnings(ready)
    assert steps[2].carried == {}
    assert steps[2].written == ()
