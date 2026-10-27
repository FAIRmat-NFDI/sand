from sand.sheet_edit.apply import apply_all, interpret_instruction
from sand.sheet_edit.edits import Anchor, Edit, NotFound, ProposedEdits, Range, Samples
from sand.sheet_edit.ground import Refused
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
        '⚠ not applied: "speed 4100"'
    )
    assert result.ready == {}


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
    assert steps[1].interpretation.preview.startswith('✗ not applied')  # no DMSO yet
    assert "holds 'IPA' on samples 1–3, not 'DMSO'" in steps[1].interpretation.preview

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


def test_the_writer_may_still_refuse_a_confirmed_edit(synthetic_xlsx):
    # the synthetic sheet has no stored formula results, so a cell a
    # formula reads cannot be written safely
    confirmed = keys(run(synthetic_xlsx, [VARIATION_SAID])[0])
    (step,) = run(synthetic_xlsx, [VARIATION_SAID], confirmed)
    assert step.written == ()
    assert step.state == synthetic_xlsx
    assert step.refused is not None
    assert 'has no stored result' in step.refused
