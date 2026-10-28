import datetime
import hashlib
import json

import pytest
from openpyxl.utils import column_index_from_string
from synthetic_sheet import letter, with_formula_results, with_part

from sand.sheet_edit.apply import apply_all
from sand.sheet_edit.changelog import (
    Changelog,
    CheckFailed,
    Confirmation,
    Entry,
    changelog,
    check,
    from_json,
    render,
    to_json,
)
from sand.sheet_edit.edits import Edit, ProposedEdits, Range, Samples
from sand.sheet_edit.write import Write, write_cells

CLEAN_SOLVENT_2, SPEED, VARIATION = 'C013', 'C025', 'C007'
SOLVENT_2_COLUMN, SPEED_COLUMN = 13, 25
DEPOSITION_COLUMN = column_index_from_string(
    letter('5: Close Space Sublimation', 'Deposition Time [s]')
)
THREE_ROWS, ALL_ROWS = 3, 12
MODEL = 'claude-sonnet-5-5 / prompt v1'
WHEN = datetime.datetime(
    2026, 10, 28, 12, 16, 5, tzinfo=datetime.timezone(datetime.timedelta(hours=2))
)
YARU = Confirmation(by='Yaru', at=WHEN)

SOLVENT = 'solvent 2 to DMSO for samples 1 to 3'
SPEED_4000, SPEED_5000 = 'speed 4000', 'speed 5000'
VARIATION_SAID = 'variation 30min for sample 1'


def edit(target, rows, old, new, said):
    return Edit(target=target, rows=rows, old=old, new=new, said=said, anchor=None)


PROPOSALS = {
    SOLVENT: [
        edit(
            CLEAN_SOLVENT_2,
            Range(first='1', last='3', subbatch=None),
            'IPA',
            'DMSO',
            SOLVENT,
        )
    ],
    SPEED_4000: [edit(SPEED, 'all', '3000', '4000', SPEED_4000)],
    SPEED_5000: [edit(SPEED, 'all', '4000', '5000', SPEED_5000)],
    VARIATION_SAID: [
        edit(
            VARIATION,
            Samples(samples=['1'], subbatch=None),
            '10min',
            '30min',
            VARIATION_SAID,
        )
    ],
}


def resolve(text, catalog):
    return ProposedEdits(edits=PROPOSALS[text], unresolved=[])


def applied(original, inputs, who=YARU):
    """Run, confirm every edit of every input (each on the sheet the
    earlier ones left), run again: the steps and the confirmations."""
    confirmations: dict[str, Confirmation] = {}
    for n in range(len(inputs)):
        steps = apply_all(original, inputs, resolve, set(confirmations), {})
        confirmations |= dict.fromkeys(steps[n].interpretation.ready, who)
    steps = apply_all(original, inputs, resolve, set(confirmations), {})
    return steps, confirmations


def logged(original, inputs, who=YARU):
    steps, confirmations = applied(original, inputs, who)
    return changelog(original, steps, confirmations, MODEL), steps[-1].state


@pytest.fixture
def with_results(synthetic_xlsx) -> bytes:
    """Row 3 with Excel-stored formula results, so the writer may
    recompute them."""
    return with_formula_results(
        synthetic_xlsx,
        {
            f'{letter("Experiment Info", "Nomad ID")}3': 'KIT_Synth_20250101_B1_0_1',
            f'{letter("5: Close Space Sublimation", "Deposition Time [s]")}3': '600',
        },
    )


# --- changelog ---------------------------------------------------------------


def test_one_entry_per_written_cell_in_order(synthetic_xlsx):
    log, final = logged(synthetic_xlsx, [SOLVENT, SPEED_4000])
    assert len(log.entries) == THREE_ROWS + ALL_ROWS
    assert log.entries[0] == Entry(
        input=1,
        cell='M3',
        row=3,
        column=SOLVENT_2_COLUMN,
        section='1: Cleaning UV-Ozone',
        header='Solvent 2',
        sample='1',
        old='IPA',
        new='DMSO',
        said=SOLVENT,
        confirmed_by='Yaru',
        confirmed_at='2026-10-28T10:16:05+00:00',
    )
    assert [e.input for e in log.entries] == [1] * THREE_ROWS + [2] * ALL_ROWS
    assert (log.entries[3].cell, log.entries[3].old, log.entries[3].new) == (
        'Y3',
        3000,
        4000,
    )
    assert log.sheet == 'Experiment Data'
    assert (log.model, log.final) == (MODEL, hashlib.sha256(final).hexdigest())


def test_an_input_nobody_confirmed_makes_no_entry(synthetic_xlsx):
    steps = apply_all(synthetic_xlsx, [SOLVENT], resolve, set(), {})
    log = changelog(synthetic_xlsx, steps, {}, MODEL)
    assert log.entries == ()
    assert log.final == log.original


def test_a_cell_edited_by_two_inputs_has_two_entries(synthetic_xlsx):
    log, _ = logged(synthetic_xlsx, [SPEED_4000, SPEED_5000])
    y3 = [(e.input, e.old, e.new) for e in log.entries if e.cell == 'Y3']
    assert y3 == [(1, 3000, 4000), (2, 4000, 5000)]


def test_a_recomputed_formula_result_is_logged_without_words(with_results):
    log, _ = logged(with_results, [VARIATION_SAID])
    variation, deposition = log.entries
    assert (variation.cell, variation.old, variation.new) == ('G3', '10min', '30min')
    assert not variation.by_formula
    assert deposition.by_formula
    assert (deposition.column, deposition.old, deposition.new) == (
        DEPOSITION_COLUMN,
        600,
        1800,
    )
    assert (deposition.said, deposition.confirmed_by, deposition.confirmed_at) == (
        None,
        None,
        None,
    )
    assert deposition.header == 'Deposition Time [s]'


def test_a_written_cell_without_a_confirmed_edit_is_a_bug(synthetic_xlsx):
    steps, _ = applied(synthetic_xlsx, [SOLVENT])
    with pytest.raises(ValueError, match='M3 was written without a confirmed edit'):
        changelog(synthetic_xlsx, steps, {}, MODEL)


# --- check ---------------------------------------------------------------------


def test_original_plus_log_is_the_final_file(synthetic_xlsx, with_results):
    log, final = logged(synthetic_xlsx, [SOLVENT, SPEED_4000, SPEED_5000])
    check(synthetic_xlsx, final, log)
    log, final = logged(with_results, [VARIATION_SAID])
    check(with_results, final, log)
    check(with_results, final, from_json(to_json(log)))  # after JSON too


def test_an_unconfirmed_input_between_two_confirmed_ones(synthetic_xlsx):
    inputs = [SPEED_4000, SOLVENT, SPEED_5000]
    steps = apply_all(synthetic_xlsx, inputs, resolve, set(), {})
    confirmations = dict.fromkeys(steps[0].interpretation.ready, YARU)
    steps = apply_all(synthetic_xlsx, inputs, resolve, set(confirmations), {})
    confirmations |= dict.fromkeys(steps[2].interpretation.ready, YARU)
    steps = apply_all(synthetic_xlsx, inputs, resolve, set(confirmations), {})
    log = changelog(synthetic_xlsx, steps, confirmations, MODEL)
    assert sorted({e.input for e in log.entries}) == [1, 3]
    y3 = [(e.input, e.old, e.new) for e in log.entries if e.cell == 'Y3']
    assert y3 == [(1, 3000, 4000), (3, 4000, 5000)]
    check(synthetic_xlsx, steps[-1].state, log)


def test_a_cleared_cell_checks(synthetic_xlsx):
    # the writer cannot be reached with an empty value from an
    # instruction yet, so the log is built by hand
    final = write_cells(synthetic_xlsx, [Write(3, SOLVENT_2_COLUMN, None)]).xlsx
    cleared = Entry(
        input=1,
        cell='M3',
        row=3,
        column=SOLVENT_2_COLUMN,
        section='1: Cleaning UV-Ozone',
        header='Solvent 2',
        sample='1',
        old='IPA',
        new=None,
        said='clear solvent 2 of sample 1',
        confirmed_by='Yaru',
        confirmed_at='2026-10-28T10:16:05+00:00',
    )
    log = Changelog(
        sheet='Experiment Data',
        original=hashlib.sha256(synthetic_xlsx).hexdigest(),
        final=hashlib.sha256(final).hexdigest(),
        model=MODEL,
        entries=(cleared,),
    )
    check(synthetic_xlsx, final, log)
    check(synthetic_xlsx, final, from_json(to_json(log)))


def test_check_catches_a_final_file_with_more_in_it(synthetic_xlsx):
    log, final = logged(synthetic_xlsx, [SOLVENT])
    more = with_part(final, 'xl/media/extra.png', b'not a picture')
    forged = Changelog(**{**log.__dict__, 'final': hashlib.sha256(more).hexdigest()})
    with pytest.raises(CheckFailed, match='differs from the original plus the log'):
        check(synthetic_xlsx, more, forged)


def test_check_catches_a_cell_logged_twice(with_results):
    log, final = logged(with_results, [VARIATION_SAID])
    twice = Changelog(**{**log.__dict__, 'entries': log.entries + log.entries[-1:]})
    with pytest.raises(CheckFailed, match='input 1: a cell is logged twice'):
        check(with_results, final, twice)


def test_check_catches_a_formula_result_logged_as_an_edit(with_results):
    log, final = logged(with_results, [VARIATION_SAID])
    as_edit = _with(
        log,
        1,
        said='deposition 1800',
        confirmed_by='Yaru',
        confirmed_at=WHEN.isoformat(),
    )
    with pytest.raises(
        CheckFailed, match='cannot be replayed: BD3 is calculated by a formula'
    ):
        check(with_results, final, as_edit)


def test_check_catches_a_changed_log_value(synthetic_xlsx):
    log, final = logged(synthetic_xlsx, [SOLVENT])
    # a changed `old`: the replay changes something else than the log says
    with pytest.raises(
        CheckFailed, match=r'log says .*M4 ETH → DMSO.*changes .*M4 IPA → DMSO'
    ):
        check(synthetic_xlsx, final, _with(log, 1, old='ETH'))
    # a changed `new`: the replay follows the log and lands on another file
    with pytest.raises(
        CheckFailed, match='differs from the original plus the log at M4'
    ):
        check(synthetic_xlsx, final, _with(log, 1, new='ETH'))


def _with(log: Changelog, index: int, **fields) -> Changelog:
    entries = list(log.entries)
    entries[index] = Entry(**{**entries[index].__dict__, **fields})
    return Changelog(**{**log.__dict__, 'entries': tuple(entries)})


def test_check_catches_a_dropped_entry(synthetic_xlsx):
    log, final = logged(synthetic_xlsx, [SOLVENT])
    shorter = Changelog(**{**log.__dict__, 'entries': log.entries[:2]})
    with pytest.raises(
        CheckFailed, match='differs from the original plus the log at M5'
    ):
        check(synthetic_xlsx, final, shorter)


def test_check_catches_the_wrong_files(synthetic_xlsx):
    log, final = logged(synthetic_xlsx, [SOLVENT])
    other = write_cells(final, [Write(6, SOLVENT_2_COLUMN, 'DMSO')]).xlsx
    with pytest.raises(CheckFailed, match='the final file is not the one'):
        check(synthetic_xlsx, other, log)
    # the hash is no protection on its own: a log made for the other file
    # still fails, because a part of the file changed that no entry covers
    forged = Changelog(**{**log.__dict__, 'final': hashlib.sha256(other).hexdigest()})
    with pytest.raises(CheckFailed, match='differs from the original plus the log'):
        check(synthetic_xlsx, other, forged)
    with pytest.raises(CheckFailed, match='the original file is not the one'):
        check(final, final, log)


# --- JSON and text ----------------------------------------------------------


def test_json_round_trip(synthetic_xlsx):
    log, _ = logged(synthetic_xlsx, [SOLVENT, SPEED_4000])
    text = to_json(log)
    assert from_json(text) == log
    assert json.loads(text)['entries'][0]['said'] == SOLVENT


def test_a_log_changed_after_writing_is_refused(synthetic_xlsx):
    log, _ = logged(synthetic_xlsx, [SOLVENT])
    data = json.loads(to_json(log))
    data['entries'][0]['new'] = 'ETH'
    with pytest.raises(CheckFailed, match='corrupt or was edited'):
        from_json(json.dumps(data))


def _digest(body):
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@pytest.mark.parametrize(
    ('text', 'message'),
    [
        ('nonsense', 'not a changelog: Expecting value'),
        ('[]', 'not a changelog: no digest'),
        ('{}', 'not a changelog: no digest'),
        ('{"digest": "x"}', 'corrupt or was edited'),
        (
            json.dumps({'sheet': 's', 'digest': _digest({'sheet': 's'})}),
            "not a changelog: KeyError\\('entries'\\)",
        ),
    ],
)
def test_anything_else_is_not_a_changelog(text, message):
    with pytest.raises(CheckFailed, match=message):
        from_json(text)


def test_a_stored_entry_with_other_fields_is_read_by_the_known_ones(synthetic_xlsx):
    log, _ = logged(synthetic_xlsx, [SOLVENT])
    data = json.loads(to_json(log))
    data.pop('digest')
    data['entries'][0]['extra'] = 1
    data['digest'] = _digest(data)
    assert from_json(json.dumps(data)) == log
    del data['entries'][0]['cell']
    data.pop('digest')
    data['digest'] = _digest(data)
    with pytest.raises(CheckFailed, match="not a changelog: KeyError\\('cell'\\)"):
        from_json(json.dumps(data))


def test_words_and_confirmation_go_together():
    fields = dict(
        input=1,
        cell='M3',
        row=3,
        column=13,
        section='s',
        header='h',
        sample='1',
        old='IPA',
        new='DMSO',
    )
    with pytest.raises(ValueError, match='words and confirmation go together'):
        Entry(**fields, said='x', confirmed_by=None, confirmed_at=None)
    with pytest.raises(ValueError, match='needs a timezone'):
        Confirmation(by='Yaru', at=datetime.datetime(2026, 10, 28, 10, 16))


def test_render(with_results):
    log, _ = logged(with_results, [VARIATION_SAID, SOLVENT])
    assert render(log) == (
        'sheet     Experiment Data\n'
        f'original  sha256 {log.original}\n'
        f'final     sha256 {log.final}\n'
        f'model     {MODEL}\n'
        '\n'
        f'1. "{VARIATION_SAID}" · confirmed by Yaru, 2026-10-28 10:16 UTC\n'
        '   G3   Experiment Info · Variation                       sample 1  10min → 30min\n'
        '\n'
        '1. by formula\n'
        '   BD3  5: Close Space Sublimation · Deposition Time [s]  sample 1  600 → 1800\n'
        '\n'
        f'2. "{SOLVENT}" · confirmed by Yaru, 2026-10-28 10:16 UTC\n'
        '   M3   1: Cleaning UV-Ozone · Solvent 2                  sample 1  IPA → DMSO\n'
        '   M4   1: Cleaning UV-Ozone · Solvent 2                  sample 2  IPA → DMSO\n'
        '   M5   1: Cleaning UV-Ozone · Solvent 2                  sample 3  IPA → DMSO'
    )


def test_render_without_changes(synthetic_xlsx):
    log = changelog(synthetic_xlsx, [], {}, MODEL)
    assert render(log).endswith('\n\nno cell changed')
