"""The evaluation's cases and judgement, without a model: every case
resolves on the synthetic sheet, and the judge counts right."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / 'evals' / 'sheet_edit'))

import run_testcases as rt  # noqa: E402

from sand.sheet_edit.apply import interpret_instruction  # noqa: E402
from sand.sheet_edit.catalog import build  # noqa: E402
from sand.sheet_edit.edits import (  # noqa: E402
    Edit,
    NotFound,
    ProposedEdits,
    Samples,
)

CASES = rt.HERE / 'testcases.json'
MIN_CASES = 20


@pytest.fixture(scope='module')
def catalog(synthetic_xlsx):
    return build(synthetic_xlsx)


@pytest.fixture(scope='module')
def cases(catalog):
    return rt.load_cases(CASES, catalog)


def test_every_case_resolves_on_the_synthetic_sheet(cases):
    assert len(cases) >= MIN_CASES
    for case in cases:
        assert case.text
        assert case.edits or case.flagged or case.unresolved or case.id == 'noop'
        for want in case.edits:
            assert want.rows, case.id


def test_an_unknown_column_or_sample_is_refused_when_loading(catalog):
    with pytest.raises(ValueError, match='0 columns match'):
        rt.column_id(2, 'Humidity', catalog)
    with pytest.raises(ValueError, match='no sample'):
        rt.rows_of(['6'], catalog)
    # a label string is not read letter by letter ("12" is not 1 and 2)
    with pytest.raises(ValueError, match='a list of sample labels'):
        rt.rows_of('12', catalog)


def _proposal(case, catalog, new=None, rows=None, said=None):
    """What a perfect model would answer for a case with one expected edit;
    `new`, `rows` and `said` make it a wrong one."""
    (want,) = case.edits
    column = next(c for c in catalog.columns if c.id == want.target)
    held = column.groups[0].value
    old = None if held is None else str(held)
    labels = [s.label for s in catalog.samples if s.row in (rows or want.rows)]
    spec = (
        'all'
        if len(labels) == len(catalog.samples)
        else Samples(samples=labels, subbatch=None)
    )
    edit = Edit(
        target=want.target,
        rows=spec,
        anchor=None,
        old=old,
        new=want.new if new is None else new,
        said=case.text if said is None else said,
    )
    return ProposedEdits(edits=[edit], unresolved=[])


def test_the_judge_counts_a_right_answer(cases, catalog, synthetic_xlsx):
    case = next(c for c in cases if c.id == 'range')
    answer = _proposal(case, catalog)
    verdict = rt.judge(
        case, interpret_instruction(case.text, synthetic_xlsx, lambda t, c: answer)
    )
    assert verdict.ok
    assert (verdict.proposed, verdict.missed, verdict.invented) == (1, 0, 0)


def test_the_judge_counts_wrong_rows_and_values(cases, catalog, synthetic_xlsx):
    case = next(c for c in cases if c.id == 'range')
    # the value check passes a number the words hold: a wrong value
    # that would be written
    wrong_value = _proposal(case, catalog, new='4400', said='rotation speed 4400')
    verdict = rt.judge(
        case,
        interpret_instruction(
            case.text + ' and rotation speed 4400',
            synthetic_xlsx,
            lambda t, c: wrong_value,
        ),
    )
    assert not verdict.ok
    assert (verdict.missed, verdict.collateral) == (1, 1)
    # the value is right but on every sample: the samples check refuses
    # it (the user named 1 to 3), so it is flagged, not written
    wrong_rows = _proposal(case, catalog, rows=tuple(s.row for s in catalog.samples))
    verdict = rt.judge(
        case, interpret_instruction(case.text, synthetic_xlsx, lambda t, c: wrong_rows)
    )
    assert not verdict.ok
    assert (verdict.flagged, verdict.missed, verdict.collateral) == (1, 0, 0)


def test_the_judge_wants_a_refusal_where_the_case_asks_one(cases, synthetic_xlsx):
    case = next(c for c in cases if c.id == 'unknown-column')
    said = NotFound(reason='not_found', said=case.text)
    nothing = ProposedEdits(edits=[], unresolved=[said])
    verdict = rt.judge(
        case, interpret_instruction(case.text, synthetic_xlsx, lambda t, c: nothing)
    )
    assert verdict.ok
    silent = ProposedEdits(edits=[], unresolved=[])
    verdict = rt.judge(
        case, interpret_instruction(case.text, synthetic_xlsx, lambda t, c: silent)
    )
    assert not verdict.ok
    assert verdict.notes == ['expected not_found or uncertain, got no unresolved item']


def test_a_flagged_column_that_gets_written_fails_the_case(
    cases, catalog, synthetic_xlsx
):
    case = next(c for c in cases if c.id == 'wrong-kind')
    (target,) = case.flagged
    edit = Edit(
        target=target, rows='all', anchor=None, old='3000', new='fast', said=case.text
    )
    answer = ProposedEdits(edits=[edit], unresolved=[])
    # ground refuses the word in a numeric column: flagged, as asked
    verdict = rt.judge(
        case, interpret_instruction(case.text, synthetic_xlsx, lambda t, c: answer)
    )
    assert verdict.ok and verdict.written == 0
    # a number the user did not say would be written: the worst outcome
    edit = Edit(
        target=target, rows='all', anchor=None, old='3000', new='4000', said='4000'
    )
    verdict = rt.judge(
        case,
        interpret_instruction(
            'step 2 rotation speed to 4000 for all samples',
            synthetic_xlsx,
            lambda t, c: ProposedEdits(edits=[edit], unresolved=[]),
        ),
    )
    assert not verdict.ok and verdict.written == 1


def test_the_summary_adds_up(cases, catalog, synthetic_xlsx):
    case = next(c for c in cases if c.id == 'range')
    answer = _proposal(case, catalog)
    good = rt.judge(
        case, interpret_instruction(case.text, synthetic_xlsx, lambda t, c: answer)
    )
    bad = rt.judge(
        case,
        interpret_instruction(
            case.text + ' and rotation speed 4400',
            synthetic_xlsx,
            lambda t, c: _proposal(
                case, catalog, new='4400', said='rotation speed 4400'
            ),
        ),
    )
    s = rt.summarize([good, bad])
    assert (s['cases'], s['passed']) == (2, 1)
    assert (s['proposed'], s['precision'], s['collateral']) == (50.0, 50.0, 1)
    text = rt.report([good, bad])
    assert text.startswith('range              ok\nrange              FAIL')
    assert 'proposed 50.0 % (target 95 %)' in text


def test_a_refused_edit_is_flagged_not_passed(cases, catalog, synthetic_xlsx):
    case = next(c for c in cases if c.id == 'range')
    # 4400 is not in the words: ground refuses it, nothing is written
    verdict = rt.judge(
        case,
        interpret_instruction(
            case.text, synthetic_xlsx, lambda t, c: _proposal(case, catalog, new='4400')
        ),
    )
    assert not verdict.ok
    assert (verdict.flagged, verdict.missed, verdict.collateral) == (1, 0, 0)
    assert rt.summarize([verdict])['flagged'] == rt.TARGETS['flagged']
    assert rt.summarize([verdict])['proposed'] == 0


def test_an_unrelated_not_found_does_not_cover_a_dropped_edit(
    cases, catalog, synthetic_xlsx
):
    case = next(c for c in cases if c.id == 'two-edits')
    speed, _ = case.edits
    edit = Edit(
        target=speed.target,
        rows=Samples(samples=['1', '2', '3', '4', '5'], subbatch=None),
        anchor=None,
        old='3000',
        new='3500',
        said='step 2 speed 3500',
    )
    other = NotFound(reason='not_found', said='for samples 1 to 5')
    answer = ProposedEdits(edits=[edit], unresolved=[other])
    verdict = rt.judge(
        case, interpret_instruction(case.text, synthetic_xlsx, lambda t, c: answer)
    )
    assert (verdict.proposed, verdict.flagged, verdict.missed) == (1, 0, 1)


def test_text_is_compared_the_way_the_job_accepts_it(cases, catalog, synthetic_xlsx):
    case = next(c for c in cases if c.id == 'note-text')
    answer = _proposal(case, catalog, new='Film looked hazy')
    verdict = rt.judge(
        case, interpret_instruction(case.text, synthetic_xlsx, lambda t, c: answer)
    )
    assert verdict.ok


def test_the_model_giving_up_is_unreadable_and_a_bad_answer_fails_the_job(
    cases, synthetic_xlsx
):
    case = next(c for c in cases if c.id == 'range')

    def gives_up(system, text, schema):
        raise rt.ModelGaveUp(rt.GAVE_UP)

    (verdict,) = rt.run([case], synthetic_xlsx, gives_up)
    assert verdict.flagged == 1 and not verdict.failed
    assert 'unreadable' in verdict.preview or 'could not be read' in verdict.preview

    def not_json(system, text, schema):
        raise rt.JobWouldFail('the answer is not JSON')

    (verdict,) = rt.run([case], synthetic_xlsx, not_json)
    assert verdict.failed and not verdict.ok
    assert rt.summarize([verdict])['failed'] == 1
    with pytest.raises(ValueError, match='no case'):
        rt.run([case], synthetic_xlsx, not_json, only='nope')
