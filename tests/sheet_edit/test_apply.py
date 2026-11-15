from sand.sheet_edit.apply import Answer, apply_all, interpret_instruction
from sand.sheet_edit.catalog import build
from sand.sheet_edit.edits import Anchor, Edit, NotFound, ProposedEdits, Range, Samples
from sand.sheet_edit.ground import Refused, read_old
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


def test_a_later_input_is_read_on_the_edits_waiting_above(synthetic_xlsx):
    """Section 4.1: input 2 sees the DMSO input 1 proposes, before
    anything is decided; confirming input 1 leaves nothing to redo."""
    llm, cache = FakeLLM(), {}
    steps = run(synthetic_xlsx, [SOLVENT, TIME], cache=cache, llm=llm)
    (ready,) = steps[1].interpretation.ready.values()
    assert ready.checks == ()
    assert steps[1].interpretation.preview.startswith(
        '1: Cleaning UV-Ozone · Time 2 [s]: 600 → 300, samples 1–3'
    )
    assert steps[1].state == synthetic_xlsx  # nothing written

    confirmed = run(synthetic_xlsx, [SOLVENT, TIME], keys(steps[0]), cache, llm)
    assert len(confirmed[0].written) == THREE_ROWS
    assert keys(confirmed[1]) == keys(steps[1])
    assert confirmed[1].written == ()  # not confirmed yet
    assert llm.calls == [SOLVENT, TIME]


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


def test_an_answer_is_reused_while_what_it_named_is_where_it_was(synthetic_xlsx):
    inputs = [SOLVENT, TIME, SPEED_SAID]
    llm, cache = FakeLLM(), {}
    first = run(synthetic_xlsx, inputs, cache=cache, llm=llm)
    assert llm.calls == inputs
    # the same inputs again, and input 1 confirmed: its sheet is the one
    # the inputs below were read on
    run(synthetic_xlsx, inputs, cache=cache, llm=llm)
    steps = run(synthetic_xlsx, inputs, keys(first[0]), cache, llm)
    assert llm.calls == inputs
    assert len(steps[2].interpretation.ready) == 1
    # input 1 rejected: input 2's DMSO is gone, it is asked again; input 3
    # names nothing that moved
    apply_all(
        synthetic_xlsx, inputs, llm, set(), cache, rejected=frozenset(keys(first[0]))
    )
    assert llm.calls[3:] == [TIME]
    # input 2 reworded: only input 2 is asked
    run(synthetic_xlsx, [SOLVENT, 'speed 3000', SPEED_SAID], keys(first[0]), cache, llm)
    assert llm.calls[4:] == ['speed 3000']


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
    # input 1 confirmed: input 2 was read on that sheet already
    one = keys(first[0])
    written = apply_all(
        synthetic_xlsx, [SOLVENT, TIME], llm, one, cache, cached_only=True
    )
    assert [s.text for s in written] == [SOLVENT, TIME]
    assert len(written[0].written) == THREE_ROWS
    # input 1 rejected: input 2's DMSO is gone, so the replay ends after 1
    rejected = apply_all(
        synthetic_xlsx,
        [SOLVENT, TIME],
        llm,
        set(),
        cache,
        cached_only=True,
        rejected=frozenset(one),
    )
    assert [s.text for s in rejected] == [SOLVENT]
    assert apply_all(synthetic_xlsx, [], llm, set(), cache, cached_only=True) == []


def in_turn(  # noqa: PLR0913
    original, inputs, confirmed=(), *, rejected=(), cache=None, llm=None
):
    return apply_all(
        original,
        inputs,
        llm or FakeLLM(),
        set(confirmed),
        {} if cache is None else cache,
        in_turn=True,
        rejected=frozenset(rejected),
    )


def test_in_turn_an_input_whose_answer_broke_is_read_in_its_turn(synthetic_xlsx):
    inputs = [SOLVENT, SPEED_SAID, TIME]
    llm, cache = FakeLLM(), {}
    first = in_turn(synthetic_xlsx, inputs, cache=cache, llm=llm)
    # the first round: each read once, on the preview it was added to
    assert llm.calls == inputs
    assert [s.waiting for s in first] == [False, False, False]
    # input 1 rejected: input 2's answer holds, it is open; input 3's
    # DMSO is gone, it waits
    one = keys(first[0])
    steps = in_turn(synthetic_xlsx, inputs, rejected=one, cache=cache, llm=llm)
    assert llm.calls[3:] == []
    assert [s.waiting for s in steps] == [False, False, True]
    # a waiting input writes nothing, shows its earlier answer and why
    assert steps[2].written == ()
    assert steps[2].interpretation.proposal == PROPOSALS[TIME]
    assert steps[2].why == (
        "Solvent 2 (1: Cleaning UV-Ozone) no longer holds 'DMSO' on samples 1–3"
    )
    # input 2 confirmed: input 3's turn
    two = keys(steps[1])
    steps = in_turn(synthetic_xlsx, inputs, two, rejected=one, cache=cache, llm=llm)
    assert llm.calls[3:] == [TIME]
    assert [s.waiting for s in steps] == [False, False, False]


def test_in_turn_a_rejection_costs_no_call(synthetic_xlsx):
    inputs = [SOLVENT, SPEED_SAID, VARIATION_SAID]
    llm, cache = FakeLLM(), {}
    first = in_turn(synthetic_xlsx, inputs, cache=cache, llm=llm)
    steps = in_turn(
        synthetic_xlsx, inputs, rejected=keys(first[0]), cache=cache, llm=llm
    )
    assert llm.calls == inputs
    assert [s.waiting for s in steps] == [False, False, False]
    # the inputs below are on another preview: new keys, the same answers
    assert keys(steps[0]) == keys(first[0])
    assert not keys(steps[1]) & keys(first[1])
    assert steps[2].interpretation.proposal == first[2].interpretation.proposal
    assert steps[2].state == synthetic_xlsx


def test_in_turn_a_new_input_is_read_on_the_sheet_it_is_added_to(synthetic_xlsx):
    llm, cache = FakeLLM(), {}
    first = in_turn(synthetic_xlsx, [SOLVENT], cache=cache, llm=llm)
    one = keys(first[0])
    # input 1 confirmed, input 2 added: read, nothing above is undecided
    steps = in_turn(synthetic_xlsx, [SOLVENT, SPEED_SAID], one, cache=cache, llm=llm)
    assert llm.calls == [SOLVENT, SPEED_SAID]
    assert not steps[1].waiting
    # input 2 undecided, input 3 added: never read, so read once
    steps = in_turn(
        synthetic_xlsx, [SOLVENT, SPEED_SAID, VARIATION_SAID], one, cache=cache, llm=llm
    )
    assert llm.calls[2:] == [VARIATION_SAID]
    assert not steps[2].waiting


def test_a_confirmed_input_under_a_reworded_one_is_held(synthetic_xlsx):
    """Finding D, section 4.1: "speed 4000" and "speed 5000" both
    confirmed, then input 1 is reworded. Input 2 is still the edit
    confirmed, 4000 -> 5000, and must not land on the 3000."""
    faster, reworded = 'speed 5000', 'set speed 4000'
    proposals = {
        **PROPOSALS,
        faster: proposal(edit(SPEED, 'all', '4000', '5000', faster)),
        reworded: proposal(edit(SPEED, 'all', '3000', '4000', SPEED_SAID)),
    }

    def resolve(text, catalog):
        return proposals[text]

    cache = {}
    first = in_turn(synthetic_xlsx, [SPEED_SAID, faster], cache=cache, llm=resolve)
    both = keys(first[0]) | keys(first[1])
    done = in_turn(synthetic_xlsx, [SPEED_SAID, faster], both, cache=cache, llm=resolve)
    assert len(done[1].written) == ALL_ROWS

    steps = in_turn(synthetic_xlsx, [reworded, faster], both, cache=cache, llm=resolve)
    assert keys(steps[1]) == keys(first[1])
    assert steps[1].held == tuple(keys(first[1]))
    assert steps[1].written == ()
    assert steps[1].state == synthetic_xlsx
    one = keys(steps[0])
    # input 1 confirmed: input 2 is written with no further click
    steps = in_turn(
        synthetic_xlsx, [reworded, faster], one | both, cache=cache, llm=resolve
    )
    assert steps[1].held == ()
    assert len(steps[1].written) == ALL_ROWS
    # input 1 rejected instead: input 2 is on another sheet and asks again
    steps = in_turn(
        synthetic_xlsx,
        [reworded, faster],
        both,
        rejected=one,
        cache=cache,
        llm=resolve,
    )
    assert not keys(steps[1]) & both
    assert (steps[1].written, steps[1].held) == ((), ())


def test_in_turn_an_input_below_an_undecided_one_waits_and_adds_nothing(
    synthetic_xlsx,
):
    cache = {}
    first = in_turn(synthetic_xlsx, [SOLVENT, SPEED_SAID, TIME], cache=cache)
    # input 1 rejected, input 2 undecided: input 3's DMSO is gone, it
    # waits with its earlier answer, out of the sheets below it
    steps = in_turn(
        synthetic_xlsx,
        [SOLVENT, SPEED_SAID, TIME, VARIATION_SAID],
        rejected=keys(first[0]),
        cache=cache,
    )
    assert steps[2].waiting
    assert steps[2].written == ()
    assert (steps[2].state, steps[2].preview) == (steps[1].state, steps[1].preview)
    assert steps[3].interpretation.catalog == build(steps[1].preview)


def test_reject_delete_and_put_back_above_cost_no_reading(synthetic_xlsx):
    inputs = [SOLVENT, SPEED_SAID, VARIATION_SAID]
    llm, cache = FakeLLM(), {}
    first = in_turn(synthetic_xlsx, inputs, cache=cache, llm=llm)
    one = keys(first[0])
    confirmed = in_turn(synthetic_xlsx, inputs, one, cache=cache, llm=llm)
    # input 1 deleted: the inputs below name nothing it wrote
    in_turn(synthetic_xlsx, inputs[1:], cache=cache, llm=llm)
    # input 1 confirmed again: the answers read with it come back
    again = in_turn(synthetic_xlsx, inputs, one, cache=cache, llm=llm)
    assert llm.calls == inputs
    assert [keys(s) for s in again] == [keys(s) for s in confirmed]


def test_a_note_whose_anchor_went_is_read_in_its_turn_and_comes_back(
    synthetic_xlsx,
):
    """'the time of the DMSO' after the DMSO note is rejected, then the
    rejection undone."""
    inputs = [SOLVENT, SPEED_SAID, TIME]
    llm, cache = FakeLLM(), {}
    first = in_turn(synthetic_xlsx, inputs, cache=cache, llm=llm)
    one = keys(first[0])
    steps = in_turn(synthetic_xlsx, inputs, rejected=one, cache=cache, llm=llm)
    assert steps[2].waiting  # DMSO gone; input 2 is open
    read = in_turn(
        synthetic_xlsx, inputs, keys(steps[1]), rejected=one, cache=cache, llm=llm
    )
    assert llm.calls[3:] == [TIME]
    assert not read[2].waiting
    # undone: the first answer, read with the DMSO, holds again
    steps = in_turn(synthetic_xlsx, inputs, keys(read[1]), cache=cache, llm=llm)
    assert llm.calls[3:] == [TIME]
    assert not steps[2].waiting
    assert keys(steps[2]) == keys(first[2])


def test_a_worked_out_value_is_never_reused(synthetic_xlsx):
    higher = 'speed 10 percent higher'
    proposals = {
        **PROPOSALS,
        higher: proposal(edit(SPEED, 'all', '3000', '3300', higher)),
    }
    calls = []

    def resolve(text, catalog):
        calls.append(text)
        return proposals[text]

    inputs, cache = [SOLVENT, higher], {}
    first = run(synthetic_xlsx, inputs, cache=cache, llm=resolve)
    run(synthetic_xlsx, inputs, keys(first[0]), cache, resolve)
    assert calls == [SOLVENT, higher]  # the same sheet
    apply_all(
        synthetic_xlsx,
        inputs,
        resolve,
        set(),
        cache,
        rejected=frozenset(keys(first[0])),
    )
    assert calls == [SOLVENT, higher, higher]


def test_a_reused_answer_is_not_checked_against_a_value_written_above(
    synthetic_xlsx,
):
    """Finding A: "speed 5000" read while the speed was 3000 ("speed
    4000" rejected); the rejection is undone and "speed 4000" confirmed.
    Its `old` 3000 is not a misreading."""
    faster = 'speed 5000'
    llm = FakeLLM()
    proposals = {
        **PROPOSALS,
        faster: proposal(edit(SPEED, 'all', '3000', '5000', faster)),
    }
    inputs, cache = [SPEED_SAID, SOLVENT, faster], {}

    def resolve(text, catalog):
        llm.calls.append(text)
        return proposals[text]

    speed = keys(run(synthetic_xlsx, [SPEED_SAID])[0])
    in_turn(synthetic_xlsx, inputs, rejected=speed, cache=cache, llm=resolve)
    steps = in_turn(synthetic_xlsx, inputs, speed, cache=cache, llm=resolve)
    assert llm.calls == inputs
    assert not steps[2].waiting  # names nothing: reused
    [ready] = steps[2].interpretation.ready.values()
    assert ready.checks == ()
    # compared live on that sheet, as before: the false check
    live = interpret_instruction(faster, steps[1].preview, resolve)
    [ready] = live.ready.values()
    assert "holds '4000'" in ready.checks[0]
    # an answer saved before reuse: it waits, with no `old` check off its sheet
    old = {key: Answer(answer.proposal) for key, answer in cache.items()}
    steps = in_turn(synthetic_xlsx, inputs, speed, cache=old, llm=resolve)
    assert steps[2].waiting
    assert steps[2].why is None
    [ready] = steps[2].interpretation.ready.values()
    assert ready.checks == ()


def test_a_misreading_stays_checked_off_its_sheet(synthetic_xlsx):
    wrong = proposal(edit(SPEED, 'all', '2000', '5000', 'speed 5000'))
    [found] = read_old(wrong, build(synthetic_xlsx))
    assert found and "not '2000'" in found[0]


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


def test_the_old_value_is_the_one_the_edit_above_leaves(synthetic_xlsx):
    """Section 1, time 600: "time 100" then "time 200" shows 100 -> 200;
    rejecting the first makes it 600 -> 200, with no call."""
    one, two = 'time 100 for samples 1 to 3', 'time 200 for samples 1 to 3'
    proposals = {
        one: proposal(edit(CLEAN_TIME_2, FIRST_THREE, '600', '100', one)),
        two: proposal(edit(CLEAN_TIME_2, FIRST_THREE, '100', '200', two)),
    }
    calls = []

    def resolve(text, catalog):
        calls.append(text)
        return proposals[text]

    cache = {}
    first = in_turn(synthetic_xlsx, [one, two], cache=cache, llm=resolve)
    assert first[1].interpretation.preview.startswith(
        '1: Cleaning UV-Ozone · Time 2 [s]: 100 → 200, samples 1–3'
    )
    rejected = in_turn(
        synthetic_xlsx, [one, two], rejected=keys(first[0]), cache=cache, llm=resolve
    )
    assert rejected[1].interpretation.preview.startswith(
        '1: Cleaning UV-Ozone · Time 2 [s]: 600 → 200, samples 1–3'
    )
    assert calls == [one, two]


def test_confirming_every_input_in_order_asks_nothing_more(synthetic_xlsx):
    inputs = [SOLVENT, TIME, SPEED_SAID]
    llm, cache = FakeLLM(), {}
    steps = in_turn(synthetic_xlsx, inputs, cache=cache, llm=llm)
    confirmed = set()
    for n in range(len(inputs)):
        confirmed |= keys(steps[n])
        steps = in_turn(synthetic_xlsx, inputs, confirmed, cache=cache, llm=llm)
        assert not any(s.waiting or s.held for s in steps)
    assert llm.calls == inputs
    assert steps[-1].state == steps[-1].preview
    assert sum(len(s.written) > 0 for s in steps) == len(inputs)


def test_a_refused_preview_write_keeps_the_confirmed_edit_in_it(
    synthetic_xlsx, monkeypatch
):
    """Finding N: input 1's confirmed edit is written, its undecided one
    the writer refuses; input 2 is still read on the written sheet."""
    from sand.sheet_edit import apply as pipeline
    from sand.sheet_edit.write import WriteRefused, write_cells

    first = run(synthetic_xlsx, [TWO, VARIATION_SAID])
    solvent, _ = first[0].interpretation.ready
    speed_column = build(synthetic_xlsx).index(SPEED)

    def refuse_speed(xlsx, writes, **options):
        if any(w.column == speed_column for w in writes):
            raise WriteRefused('the speed cell is locked')
        return write_cells(xlsx, writes, **options)

    monkeypatch.setattr(pipeline, 'write_cells', refuse_speed)
    steps = run(synthetic_xlsx, [TWO, VARIATION_SAID], {solvent})
    assert len(steps[0].written) == THREE_ROWS
    assert steps[0].preview == steps[0].state
    assert steps[1].interpretation.catalog == build(steps[0].state)


def test_a_key_two_inputs_share_is_confirmed_for_the_first_only(
    synthetic_xlsx, monkeypatch
):
    """The same words on the same preview share a key (here: the writer
    refuses every write, so the preview stays the original). The server
    records the confirmation as the first input's."""
    from sand.sheet_edit import apply as pipeline
    from sand.sheet_edit.write import WriteRefused

    def refuse(xlsx, writes, **options):
        raise WriteRefused('the cell is locked')

    monkeypatch.setattr(pipeline, 'write_cells', refuse)
    first = run(synthetic_xlsx, [SPEED_SAID, SPEED_SAID])
    assert keys(first[0]) == keys(first[1])
    steps = run(synthetic_xlsx, [SPEED_SAID, SPEED_SAID], keys(first[0]))
    assert steps[0].confirmed == tuple(keys(first[0]))
    assert steps[1].confirmed == ()
