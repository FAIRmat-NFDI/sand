import datetime

import pytest
from synthetic_sheet import edited, letter, with_formula_results

from sand.sheet_edit.catalog import build
from sand.sheet_edit.edits import Anchor, Edit, Range, Samples, Where
from sand.sheet_edit.ground import (
    Grounded,
    Refusal,
    Refused,
    ground,
    resolve_rows,
    said_samples,
    same_value,
    sample_by_label,
)

INFO = 'Experiment Info'
SPEED, VARIATION, DEPOSITION, SUBSTRATE, NOTES = 'C025', 'C007', 'C056', 'C055', 'C029'
DATE, AREA, TIME, ANNEAL_TIME = 'C001', 'C008', 'C026', 'C027'
CLEAN_SOLVENT_1, CLEAN_TIME_1, CLEAN_SOLVENT_2, CLEAN_TIME_2 = (
    'C010',
    'C011',
    'C013',
    'C014',
)
VOLUME_3 = 'C034'  # 3: Spin Coating, Solvent 1 volume [uL], 50
ALL_ROWS = tuple(range(3, 15))  # samples 1-5, 7-13


@pytest.fixture(scope='module')
def catalog(synthetic_xlsx):
    return build(synthetic_xlsx)


@pytest.fixture(scope='module')
def two_subbatches(synthetic_xlsx):
    subbatch, sample = letter(INFO, 'Subbatch'), letter(INFO, 'Sample')

    def change(ws):  # rows 9-14 become subbatch 1, samples 1-6
        for i, r in enumerate(range(9, 15)):
            ws[f'{subbatch}{r}'] = 1
            ws[f'{sample}{r}'] = i + 1

    return build(edited(synthetic_xlsx, change))


@pytest.fixture(scope='module')
def prefixed(synthetic_xlsx):
    sample = letter(INFO, 'Sample')

    def change(ws):  # labels s_1..s_5, s_7..s_13
        for r in range(3, 15):
            ws[f'{sample}{r}'] = f's_{ws[f"{sample}{r}"].value}'

    return build(edited(synthetic_xlsx, change))


@pytest.fixture(scope='module')
def words(synthetic_xlsx):
    sample = letter(INFO, 'Sample')
    names = ['A', 'B', 'C', 'D', 'E', 'ref', 'hot', 'cold', 'Ctrl', 'Ctrl-2', 'x', 'K']

    def change(ws):
        for r, name in zip(range(3, 15), names, strict=True):
            ws[f'{sample}{r}'] = name

    return build(edited(synthetic_xlsx, change))


def edit(target=SPEED, rows='all', **fields):
    fields = {
        'anchor': None,
        'old': '3000',
        'new': '4000',
        'said': 'spin coating 2 speed 4000',
        **fields,
    }
    return Edit(target=target, rows=rows, **fields)


def samples(*labels, subbatch=None):
    return Samples(samples=list(labels), subbatch=subbatch)


def between(first, last, subbatch=None):
    return Range(first=first, last=last, subbatch=subbatch)


def refused(result) -> str:
    assert isinstance(result, Refused), result
    return ' | '.join(result.reasons)


# --- W writable ---------------------------------------------------------------


def test_a_formula_column_is_refused_naming_what_to_edit_instead(catalog):
    why = refused(ground(edit(target=DEPOSITION, said='deposition time 3000'), catalog))
    assert why == (
        'Deposition Time [s] (5: Close Space Sublimation) is calculated by a '
        'formula from Variation; edit Variation instead'
    )


# --- R rows -------------------------------------------------------------------


def test_all_means_every_sample_row(catalog):
    result = ground(edit(), catalog)
    assert isinstance(result, Grounded)
    assert (result.rows, result.unchanged, result.notes, result.status) == (
        ALL_ROWS,
        (),
        (),
        'ok',
    )


def test_listed_samples_in_sheet_order(catalog):
    result = ground(
        edit(rows=samples('7', '1', '3'), said='speed 4000 for samples 1, 3 and 7'),
        catalog,
    )
    assert result.rows == (3, 5, 8)


def test_a_range_is_expanded_by_code_and_a_gap_is_reported(catalog):
    result = ground(
        edit(rows=between('4', '8'), said='speed 4000 for samples 4 to 8'), catalog
    )
    assert result.rows == (6, 7, 8, 9)  # samples 4, 5, 7, 8: there is no 6
    assert result.notes == ('samples 4 to 8: no sample 6, applied to 4 samples',)


def test_a_range_without_a_gap_has_no_note(catalog):
    result = ground(
        edit(rows=between('1', '5'), said='speed 4000 for samples 1 to 5'), catalog
    )
    assert (result.rows, result.notes) == ((3, 4, 5, 6, 7), ())


def test_a_backwards_range_is_refused(catalog):
    why = refused(ground(edit(rows=between('8', '4'), said='samples 8 to 4'), catalog))
    assert why == 'samples 8 to 4: 8 comes after 4 in the sheet'


def test_sample_by_label_finds_the_row(catalog, two_subbatches):
    found = sample_by_label(catalog, '7', None)
    assert (found.row, found.label) == (8, '7')  # row 8: there is no sample 6
    found = sample_by_label(two_subbatches, '1', '1')
    assert (found.row, found.label, found.subbatch) == (9, '1', '1')


@pytest.mark.parametrize(
    ('label', 'subbatch', 'why'),
    [
        ('8', None, 'there is no sample 8'),
        ('7', '1', 'there is no sample 7 in subbatch 1'),  # 7 is in subbatch 0 only
        ('1', '5', 'there is no subbatch 5'),
        ('1', None, 'sample 1 is in subbatches 0, 1: say which one'),
    ],
)
def test_sample_by_label_refusals(two_subbatches, label, subbatch, why):
    with pytest.raises(Refusal, match=f'^{why}$'):
        sample_by_label(two_subbatches, label, subbatch)


@pytest.mark.parametrize(
    ('spec', 'rows', 'notes'),
    [
        ('all', ALL_ROWS, ()),
        (Samples(samples=['7', '1', '1'], subbatch=None), (3, 8), ()),
        (Range(first='1', last='3', subbatch=None), (3, 4, 5), ()),
        (
            Range(first='5', last='8', subbatch=None),
            (7, 8, 9),
            ('samples 5 to 8: no sample 6, applied to 3 samples',),
        ),
        (Where(where=VARIATION, equals='20min'), (6, 7, 8), ()),
    ],
)
def test_resolve_rows(catalog, spec, rows, notes):
    assert resolve_rows(spec, catalog) == (rows, notes)


def test_a_missing_sample_is_refused(catalog):
    assert refused(ground(edit(rows=samples('6'), said='sample 6'), catalog)) == (
        'there is no sample 6'
    )
    assert refused(
        ground(edit(rows=between('1', '6'), said='samples 1 to 6'), catalog)
    ) == ('there is no sample 6')


def test_where_matches_the_value_exactly(catalog):
    # '20min' is not '20min-hot'
    result = ground(
        edit(
            rows=Where(where=VARIATION, equals='20min'),
            said='speed 4000 where variation is 20min',
        ),
        catalog,
    )
    assert result.rows == (6, 7, 8)  # samples 4, 5, 7


def test_where_with_no_match_lists_what_the_column_holds(catalog):
    why = refused(
        ground(
            edit(rows=Where(where=VARIATION, equals='30min'), said='variation 30min'),
            catalog,
        )
    )
    assert why == (
        "no sample has Variation (Experiment Info) = '30min'; "
        "it holds '10min', '20min', '10min-hot', '20min-hot'"
    )


def test_where_empty_selects_the_empty_cells(catalog):
    result = ground(
        edit(
            rows=Where(where=NOTES, equals=None),
            said='speed 4000 where notes are empty',
        ),
        catalog,
    )
    assert result.rows == ALL_ROWS


# --- subbatches ---------------------------------------------------------------


def test_a_label_in_two_subbatches_needs_the_subbatch(two_subbatches):
    why = refused(
        ground(edit(rows=samples('1'), said='speed 4000 for sample 1'), two_subbatches)
    )
    assert why == 'sample 1 is in subbatches 0, 1: say which one'


def test_a_named_subbatch_picks_the_row(two_subbatches):
    result = ground(
        edit(
            rows=samples('1', subbatch='1'),
            said='speed 4000 for sample 1 of subbatch 1',
        ),
        two_subbatches,
    )
    assert result.rows == (9,)


def test_a_range_stays_inside_its_subbatch(two_subbatches):
    result = ground(
        edit(
            rows=between('1', '3', subbatch='1'),
            said='speed 4000 for samples 1 to 3 of subbatch 1',
        ),
        two_subbatches,
    )
    assert result.rows == (9, 10, 11)


def test_a_different_subbatch_in_the_words_is_refused(two_subbatches):
    why = refused(
        ground(
            edit(
                rows=samples('1', subbatch='1'),
                said='speed 4000 for sample 1 of subbatch 0',
            ),
            two_subbatches,
        )
    )
    assert why == 'you named subbatch 0, not 1'


def test_a_subbatch_the_edit_names_but_the_words_do_not_is_a_doubt(two_subbatches):
    result = ground(
        edit(rows=samples('1', subbatch='1'), said='speed 4000 for sample 1'),
        two_subbatches,
    )
    assert result.rows == (9,)
    assert result.doubts == ('subbatch 1: not named in your words',)
    assert result.confidence == 'low'


def test_an_unknown_subbatch_is_refused(two_subbatches):
    why = refused(
        ground(
            edit(rows=samples('1', subbatch='5'), said='sample 1 of subbatch 5'),
            two_subbatches,
        )
    )
    assert why == 'there is no subbatch 5'


def test_a_label_in_one_subbatch_needs_no_subbatch(two_subbatches):
    result = ground(
        edit(rows=samples('7'), said='speed 4000 for sample 7'), two_subbatches
    )
    assert result.rows == (8,)  # only subbatch 0 has a sample 7


# --- S sample words -----------------------------------------------------------


def test_naming_samples_but_answering_all_is_refused(catalog):
    why = refused(
        ground(edit(rows='all', said='speed 4000 for samples 1 to 3'), catalog)
    )
    assert why == 'you named samples 1–3, but the edit would apply to all samples'


def test_naming_samples_but_answering_a_condition_is_refused(catalog):
    why = refused(
        ground(
            edit(
                rows=Where(where=VARIATION, equals='10min'),
                said='speed 4000 for samples 1 to 3',
            ),
            catalog,
        )
    )
    assert (
        why
        == 'you named samples 1–3, but the edit would apply to samples chosen by a value'
    )


def test_naming_a_subbatch_but_answering_all_is_refused(two_subbatches, catalog):
    why = refused(
        ground(
            edit(rows='all', said='speed 4000 for all samples of subbatch 1'),
            two_subbatches,
        )
    )
    assert why == (
        'you named subbatch 1, but the edit would apply to all samples of every subbatch'
    )
    # one subbatch in the sheet: naming it changes nothing
    result = ground(
        edit(rows='all', said='speed 4000 for all samples of subbatch 0'), catalog
    )
    assert result.rows == ALL_ROWS


def test_an_unknown_subbatch_in_the_words_is_refused(catalog, two_subbatches):
    said = 'speed 4000 for all samples of subbatch 5'
    assert (
        refused(ground(edit(rows='all', said=said), catalog))
        == 'there is no subbatch 5'
    )
    said = 'speed 4000 for sample 7 of subbatch 5'  # 7 exists, in subbatch 0 only
    why = refused(ground(edit(rows=samples('7'), said=said), two_subbatches))
    assert why == 'there is no subbatch 5'


def test_a_label_in_two_subbatches_names_both(two_subbatches):
    why = refused(
        ground(edit(rows='all', said='speed 4000 for sample 1'), two_subbatches)
    )
    assert why == (
        'you named samples 1 of subbatch 0, 1 of subbatch 1, '
        'but the edit would apply to all samples'
    )


def test_a_partly_named_range_is_refused(catalog):
    why = refused(
        ground(
            edit(rows=between('1', '3'), said='speed 4000 for samples 1 and 2'), catalog
        )
    )
    assert why == 'the edit includes samples 3, which you did not name'


def test_a_named_sample_that_does_not_exist_is_refused(catalog):
    why = refused(
        ground(
            edit(rows=samples('5', '7'), said='speed 4000 for samples 5, 6 and 7'),
            catalog,
        )
    )
    assert why == 'there is no sample 6'


def test_a_sample_without_a_label_is_not_named_by_a_separator(synthetic_xlsx):
    sample = letter(INFO, 'Sample')
    catalog = build(
        edited(synthetic_xlsx, lambda ws: ws.__setitem__(f'{sample}3', None))
    )
    result = ground(edit(rows=samples('2', '3'), said='samples: 2 , 3'), catalog)
    assert result.rows == (4, 5)


def test_a_sample_without_a_label_is_named_by_its_row(synthetic_xlsx):
    sample, nomad_id = letter(INFO, 'Sample'), letter(INFO, 'Nomad ID')
    no_label = edited(synthetic_xlsx, lambda ws: ws.__setitem__(f'{sample}3', None))
    catalog = build(with_formula_results(no_label, {f'{nomad_id}3': 'KIT_Synth_1'}))
    why = refused(ground(edit(rows='all', said='speed 4000 for KIT_Synth_1'), catalog))
    assert why == 'you named samples row 3, but the edit would apply to all samples'


def test_a_leading_zero_is_the_same_number(synthetic_xlsx):
    sample = letter(INFO, 'Sample')
    catalog = build(
        edited(synthetic_xlsx, lambda ws: ws.__setitem__(f'{sample}9', '08'))
    )
    for said in ('sample 8', 'sample 08'):
        assert [s.row for s in said_samples(said, catalog).samples] == [9]


def test_a_range_the_user_did_not_name_is_a_doubt(catalog):
    # code found no sample words: no contradiction, the user confirms once
    result = ground(
        edit(rows=between('1', '3'), said='speed 4000 for the first three samples'),
        catalog,
    )
    assert result.rows == (3, 4, 5)
    assert result.doubts == ('samples 1 to 3: not named in your words',)
    assert result.confidence == 'low'


def test_a_named_sample_the_edit_leaves_out_is_refused(catalog):
    why = refused(
        ground(
            edit(rows=samples('1', '3'), said='speed 4000 for samples 1, 3 and 7'),
            catalog,
        )
    )
    assert why == 'you named samples 7, which the edit does not include'


def test_a_sample_the_user_did_not_name_is_refused(catalog):
    why = refused(
        ground(edit(rows=samples('1', '7'), said='speed 4000 for sample 1'), catalog)
    )
    assert why == 'the edit includes samples 7, which you did not name'


def test_a_value_right_after_the_list_is_not_a_sample(catalog):
    for said in (
        'samples 1 to 3 4000 rpm',
        'samples 1 to 3 to 4000',
        'samples 1 to 3: 4000',
    ):
        result = ground(edit(rows=between('1', '3'), said=said), catalog)
        assert isinstance(result, Grounded), (said, result)
        assert result.rows == (3, 4, 5), said
    result = ground(
        edit(
            ANNEAL_TIME,
            between('1', '3'),
            old='10',
            new='15 minutes',
            said='samples 1 to 3 15 minutes',
        ),
        catalog,
    )
    assert (result.rows, result.value) == ((3, 4, 5), 15)


def test_except_takes_samples_out(catalog):
    said = 'speed 4000 for samples 1 to 7 except 3'
    result = ground(edit(rows=samples('1', '2', '4', '5', '7'), said=said), catalog)
    assert result.rows == (3, 4, 6, 7, 8)
    why = refused(
        ground(edit(rows=samples('1', '2', '3', '4', '5', '7'), said=said), catalog)
    )
    assert why.startswith('you excluded samples 3, but the edit includes them')
    why = refused(ground(edit(rows='all', said='all samples except 3'), catalog))
    assert why == 'you excluded samples 3, but the edit would apply to all samples'
    rest = [s.label for s in catalog.samples if s.label != '3']
    result = ground(edit(rows=samples(*rest), said='all samples except 3'), catalog)
    row_of_3 = sample_by_label(catalog, '3', None).row
    assert result.rows == tuple(r for r in ALL_ROWS if r != row_of_3)


def test_all_samples_of_a_subbatch(two_subbatches):
    result = ground(
        edit(
            rows=between('1', '6', subbatch='1'),
            said='4000 for all samples of subbatch 1',
        ),
        two_subbatches,
    )
    assert result.rows == (9, 10, 11, 12, 13, 14)


def test_a_range_over_two_subbatches_is_one_edit_each(two_subbatches):
    said = 'speed 4000 for samples 1 to 3 of subbatches 0 and 1'
    for subbatch, rows in (('0', (3, 4, 5)), ('1', (9, 10, 11))):
        result = ground(
            edit(rows=between('1', '3', subbatch=subbatch), said=said), two_subbatches
        )
        assert result.rows == rows, subbatch
    assert said_samples(said, two_subbatches).subbatches == ('0', '1')
    assert said_samples("subbatch 1's samples", two_subbatches).subbatches == ('1',)


def test_the_same_samples_in_another_form_are_fine(catalog):
    # the user listed them; the LLM answered a range over the same rows
    result = ground(
        edit(rows=between('1', '3'), said='speed 4000 for samples 1, 2 and 3'), catalog
    )
    assert result.rows == (3, 4, 5)
    result = ground(
        edit(rows=samples('1', '2', '3'), said='speed 4000 for samples 1 to 3'), catalog
    )
    assert result.rows == (3, 4, 5)


def test_a_nomad_id_counts_as_naming_the_sample(synthetic_xlsx):
    nomad_id = letter(INFO, 'Nomad ID')
    catalog = build(
        with_formula_results(synthetic_xlsx, {f'{nomad_id}3': 'KIT_Synth_1'})
    )
    result = ground(
        edit(rows=samples('1'), said='speed 4000 for sample KIT_Synth_1'), catalog
    )
    assert result.rows == (3,)


@pytest.mark.parametrize(
    ('said', 'labels'),
    [
        ('speed 4000 for samples 1-3 and 7', ['1', '2', '3', '7']),
        ('samples 1, 3 and 7 to 9', ['1', '3', '7', '8', '9']),
        ('sample seven', ['7']),
        ('samples 1 and sample 3', ['1', '3']),
        ('sample 13', ['13']),
        ('sample no. 3', ['3']),
        ('samples: 1 to 3', ['1', '2', '3']),
        ('samples 1 , 3', ['1', '3']),
        ('sample1 and sample2', ['1', '2']),
        ('sample 1 20 min', ['1']),  # the value, not sample 20
        # the value after 'to' is not a range end
        ('samples 1 and 2 to 3000', ['1', '2']),
        ('samples 1, 2 and 7 to 4000', ['1', '2', '7']),
        ('sample 1 of subbatch 0', ['1']),
        ('samples with variation 15min', []),  # a condition, not sample numbers
        ('the 20min samples', []),
        ('set 4000 for all samples', []),
        ('sample 6', []),  # there is none: see `unknown`
        ('speed 4000', []),
    ],
)
def test_said_samples(catalog, said, labels):
    assert [s.label for s in said_samples(said, catalog).samples] == labels


def test_prefixed_labels(prefixed):
    assert prefixed.names((3, 4, 5, 7, 8)) == 's_1–s_3, s_5, s_7'
    assert sample_by_label(prefixed, 's_7', None).row == 8  # noqa: PLR2004
    cases = {
        'speed 4000 for s_1, s_2 and s_7': ['s_1', 's_2', 's_7'],
        'samples s_1 to s_3': ['s_1', 's_2', 's_3'],
        'samples 1 to 3': ['s_1', 's_2', 's_3'],  # the number names the sample
        'samples 1 and 2 to 3000': ['s_1', 's_2'],
        'sample seven': ['s_7'],
    }
    for said, labels in cases.items():
        assert [s.label for s in said_samples(said, prefixed).samples] == labels, said
    assert said_samples('sample s_6', prefixed).unknown == ('s_6',)
    result = ground(
        edit(rows=between('s_1', 's_3'), said='speed 4000 for samples 1 to 3'), prefixed
    )
    assert result.rows == (3, 4, 5)


def test_word_labels(words):
    assert words.names((3, 4, 5, 8)) == 'A, B, C, ref'
    cases = {
        'speed 4000 for samples A, B and ref': ['A', 'B', 'ref'],
        'samples A to C': ['A', 'B', 'C'],  # sheet order
        'sample hot and sample cold': ['hot', 'cold'],
        'sample Ctrl-2': ['Ctrl-2'],  # not also Ctrl
        'samples a and b': ['A', 'B'],  # any spelling when only one label fits
        'the reference sample': [],  # not a label: nothing found, nothing guessed
        'sample X': ['x'],
        'speed 4000 for a sample': [],  # 'a' is an article, not sample A
        'A and B': [],  # one-letter labels only count after 'sample(s)'
        'ref and hot': ['ref', 'hot'],
    }
    for said, labels in cases.items():
        assert [s.label for s in said_samples(said, words).samples] == labels, said
    result = ground(
        edit(rows=between('A', 'C'), said='speed 4000 for samples A to C'), words
    )
    assert result.rows == (3, 4, 5)


@pytest.mark.parametrize(
    ('said', 'unknown'),
    [
        ('samples 5, 6 and 7', ('6',)),
        ('sample 6', ('6',)),
        ('samples 1 and 2 to 3000', ()),  # the value is not a sample
        ('samples 1 to 3 4000 rpm', ()),
        ('samples 1 to 3 to 4000', ()),
        ('samples 1 to 3', ()),
        ('samples with variation 15min', ()),
    ],
)
def test_said_samples_unknown(catalog, said, unknown):
    assert said_samples(said, catalog).unknown == unknown


# --- O old value ------------------------------------------------------------------


def test_old_must_be_what_the_cell_holds(catalog):
    why = refused(ground(edit(old='2000'), catalog))
    assert why == (
        "Rotation speed [rpm] (2: Spin Coating) holds '3000' on samples 1–5, 7–13, "
        "not '2000'"
    )
    why = refused(ground(edit(old=None), catalog))
    assert why.endswith('not empty')


def test_old_describes_the_rows_still_to_change(catalog):
    # samples 1-5, 7 already hold 120: `old` is 115, the value on the others
    result = ground(
        edit(target=SUBSTRATE, old='115', new='120', said='substrate temperature 120'),
        catalog,
    )
    assert isinstance(result, Grounded)
    why = refused(
        ground(
            edit(
                target=SUBSTRATE, old='110', new='120', said='substrate temperature 120'
            ),
            catalog,
        )
    )
    assert why.startswith(
        "Substrate temperature [°C] (5: Close Space Sublimation) holds '115'"
    )


# --- A anchor ---------------------------------------------------------------------


def ipa(target, equals='IPA', said='time of the IPA 300'):
    return edit(
        target,
        anchor=Anchor(column=CLEAN_SOLVENT_2, equals=equals),
        old='600',
        new='300',
        said=said,
    )


def test_an_anchor_reaches_its_own_number(catalog):
    result = ground(ipa(CLEAN_TIME_2), catalog)  # Solvent 2 -> Time 2
    assert result.rows == ALL_ROWS
    assert result.confidence == 'high'
    why = refused(ground(ipa(CLEAN_TIME_1), catalog))  # Solvent 2 -> Time 1
    assert why == 'Solvent 2 is number 2, Time 1 [s] is number 1'


def test_an_anchor_must_hold_its_value(catalog):
    why = refused(
        ground(ipa(CLEAN_TIME_2, equals='DMSO', said='time of the DMSO 300'), catalog)
    )
    assert why == (
        "Solvent 2 (1: Cleaning UV-Ozone) holds 'IPA' on samples 1–5, 7–13, not 'DMSO'"
    )


def test_an_anchor_stays_in_its_step(catalog):
    why = refused(ground(ipa(SPEED, said='speed of the IPA 300'), catalog))
    assert why.startswith(
        'Solvent 2 (1: Cleaning UV-Ozone) is not in the step of Rotation speed'
    )


def test_an_anchor_value_missing_from_the_words_is_a_doubt(catalog):
    result = ground(ipa(CLEAN_TIME_2, said='second cleaning time 300'), catalog)
    assert result.doubts == ("'IPA': not found in your words",)


# --- V value in the words ---------------------------------------------------------


def test_a_number_not_said_is_refused(catalog):
    why = refused(ground(edit(new='4100', said='speed 4000'), catalog))
    assert why == (
        "Rotation speed [rpm] (2: Spin Coating): '4100' is not in your words; you said 4000"
    )
    why = refused(ground(edit(new='4000', said='speed 4 krpm'), catalog))
    assert why.endswith('you said 4 krpm')  # the LLM converted: refused, see #108


def test_a_spoken_number_is_a_note(catalog):
    result = ground(edit(said='speed four thousand'), catalog)
    assert result.notes == (
        "Rotation speed [rpm] (2: Spin Coating): '4000': read from 'four thousand'",
    )
    assert result.confidence == 'medium'


def test_no_number_in_the_words_is_a_doubt(catalog):
    result = ground(edit(said='set the speed to the usual'), catalog)
    assert result.rows == ALL_ROWS
    assert result.doubts == (
        "Rotation speed [rpm] (2: Spin Coating): '4000' not found in your words",
    )
    assert result.confidence == 'low'


def test_numbers_of_the_column_name_are_not_values(catalog):
    # the 2 of 'spin coating 2' and the 1 of 'solvent 1'
    assert ground(edit(said='spin coating 2 speed 4000'), catalog).confidence == 'high'
    result = ground(
        edit(VOLUME_3, old='50', new='100', said='step 3 solvent 1 volume 100'), catalog
    )
    assert (result.confidence, result.value) == ('high', 100)
    why = refused(
        ground(
            edit(VOLUME_3, old='50', new='1', said='step 3 solvent 1 volume 100'),
            catalog,
        )
    )
    assert why.endswith('you said 100')


def test_a_value_with_a_unit_is_never_an_index(catalog):
    rate = 'C050'  # Rate 2 [angstrom/s], 1
    result = ground(
        edit(
            rate,
            old='1',
            new='2 angstrom per second',
            said='rate 2 angstrom per second',
        ),
        catalog,
    )
    assert (result.confidence, result.value) == ('high', 2)


def test_text_matches_across_spaces_and_hyphens(catalog):
    material = 'C017'  # Material name, Me-4PACz
    result = ground(
        edit(material, old='Me-4PACz', new='2PACz', said='material name to 2 PACz'),
        catalog,
    )
    assert result.confidence == 'high'


def test_text_is_looked_for_as_written(catalog):
    result = ground(
        edit(
            CLEAN_SOLVENT_1, old='Acetone', new='IPA', said='first cleaning solvent ipa'
        ),
        catalog,
    )
    assert (result.confidence, result.value) == ('high', 'IPA')
    result = ground(
        edit(
            CLEAN_SOLVENT_1,
            old='Acetone',
            new='IPA',
            said='first cleaning solvent isopropanol',
        ),
        catalog,
    )
    assert result.doubts == (
        "Solvent 1 (1: Cleaning UV-Ozone): 'IPA' not found in your words",
    )


def test_text_is_not_compared_as_a_number(catalog):
    # '15min' is text: '15 minutes' is not it as written, '15 MIN' is
    cases = (
        ('variation of sample 1 15 minutes', 'low'),
        ('variation of sample 1 15 MIN', 'high'),
    )
    for said, confidence in cases:
        result = ground(
            edit(VARIATION, rows=samples('1'), old='10min', new='15min', said=said),
            catalog,
        )
        assert result.confidence == confidence, said


def test_yes_is_a_true_word(synthetic_xlsx):
    flag = letter(INFO, 'Notes')  # turned into a TRUE/FALSE column

    def change(ws):
        for r in range(3, 15):
            ws[f'{flag}{r}'] = False

    catalog = build(edited(synthetic_xlsx, change))
    notes = 'C009'
    result = ground(edit(notes, old='FALSE', new='YES', said='flag yes'), catalog)
    assert (result.value, result.confidence) == (True, 'high')
    result = ground(edit(notes, old='FALSE', new='YES', said='flag no'), catalog)
    assert result.confidence == 'low'


def test_clearing_needs_a_clearing_word(catalog):
    tool = 'C019'  # Tool/GB name, 'box1'
    result = ground(edit(tool, old='box1', new='', said='clear the tool name'), catalog)
    assert (result.value, result.confidence) == (None, 'high')
    result = ground(edit(tool, old='box1', new='', said='the tool name'), catalog)
    assert result.confidence == 'low'


# --- T type and U unit ------------------------------------------------------------


def test_a_unit_must_be_the_columns(catalog):
    why = refused(ground(edit(new='4 krpm', said='speed 4 krpm'), catalog))
    assert why == "'4 krpm' is in krpm, not a [rpm] column"
    why = refused(
        ground(
            edit(TIME, old='30', new='15 minutes', said='rotation time 15 minutes'),
            catalog,
        )
    )
    assert why == "'15 minutes' is in min, not a [s] column"
    result = ground(
        edit(TIME, old='30', new='15 seconds', said='rotation time 15 seconds'), catalog
    )
    assert (result.rows, result.value) == (ALL_ROWS, 15)  # the unit is stripped


def test_text_into_a_number_column_is_refused(catalog):
    why = refused(ground(edit(new='fast', said='speed fast'), catalog))
    assert (
        why
        == "Rotation speed [rpm] (2: Spin Coating) holds numbers; 'fast' is not a number"
    )


def test_values_are_written_in_the_columns_type(catalog):
    written = [
        ground(edit(new='4000 rpm', said='speed 4000 rpm'), catalog).value,
        ground(edit(new='3500.5', said='speed 3500.5'), catalog).value,
        ground(
            edit(AREA, old='0.1', new='0.15', said='sample area 0.15'), catalog
        ).value,
    ]
    assert written == [4000, 3500.5, 0.15]
    result = ground(
        edit(
            VARIATION,
            old='10min',
            new='15min',
            rows=samples('1'),
            said='variation of sample 1 15min',
        ),
        catalog,
    )
    assert result.value == '15min'  # text stays text
    result = ground(
        edit(
            VARIATION,
            old='10min',
            new='15',
            rows=samples('1'),
            said='variation of sample 1 to 15',
        ),
        catalog,
    )
    assert result.value == '15'


def test_dates_keep_the_sheets_form(catalog):
    result = ground(
        edit(DATE, old='20250101', new='20250102', said='date 20250102'), catalog
    )
    assert (result.rows, result.value) == (ALL_ROWS, 20250102)
    for said in ('date 20250132', 'date 2025-02-02'):
        why = refused(
            ground(edit(DATE, old='20250101', new=said[5:], said=said), catalog)
        )
        assert why == (
            f"Date (Experiment Info) holds dates written as 20250101; '{said[5:]}' is not one"
        )
    why = refused(
        ground(
            edit(DATE, old='20250101', new='20250202', said='date 20250201'), catalog
        )
    )
    assert (
        why
        == "Date (Experiment Info): '20250202' is not in your words; you said 20250201"
    )
    # a date said in words: code does not read it yet, the user confirms once
    result = ground(
        edit(DATE, old='20250101', new='20250202', said='date 2nd of February 2025'),
        catalog,
    )
    assert result.confidence == 'low'
    assert result.value == int('20250202')


# --- confidence -------------------------------------------------------------------


def test_confidence_levels(catalog):
    high = ground(
        edit(rows=between('1', '3'), said='speed 4000 for samples 1 to 3'), catalog
    )
    medium = ground(
        edit(rows=between('5', '8'), said='speed 4000 for samples 5 to 8'), catalog
    )
    low = ground(
        edit(rows=between('1', '3'), said='speed 4000 for the first three'), catalog
    )
    assert (high.confidence, medium.confidence, low.confidence) == (
        'high',
        'medium',
        'low',
    )
    assert medium.notes == ('samples 5 to 8: no sample 6, applied to 3 samples',)


# --- N no-op --------------------------------------------------------------------


def test_a_value_already_everywhere_is_a_warning_not_an_error(catalog):
    result = ground(edit(new='3000', said='speed 3000'), catalog)
    assert isinstance(result, Grounded)
    assert (result.status, result.rows, result.unchanged) == ('noop', (), ALL_ROWS)
    assert result.notes == (
        'Rotation speed [rpm] (2: Spin Coating): already 3000 on all 12 samples, '
        'nothing to change',
    )


def test_rows_already_holding_the_value_are_left_out(catalog):
    result = ground(
        edit(target=SUBSTRATE, old='115', new='120', said='substrate temperature 120'),
        catalog,
    )
    assert result.status == 'ok'
    assert result.rows == (9, 10, 11, 12, 13, 14)  # samples 8-13 hold 115
    assert result.unchanged == (3, 4, 5, 6, 7, 8)
    assert result.notes == (
        'Substrate temperature [°C] (5: Close Space Sublimation): already 120 on '
        'samples 1–5, 7, nothing to change',
    )


@pytest.mark.parametrize(
    ('new', 'stored', 'same'),
    [
        ('3000', 3000, True),
        ('3000', 3000.0, True),
        ('3,000', 3000, True),
        ('four thousand', 4000, True),
        ('3000 rpm', 3000, True),  # the unit is check U's business
        ('3001', 3000, False),
        ('3000', '3000', True),  # text cells compare as text
        ('20min', '20min-hot', False),
        ('Ethanol', 'ethanol', False),
        (' Ethanol ', 'Ethanol', True),
        ('', None, True),
        ('x', None, False),
        ('TRUE', True, True),
        ('1', True, False),
        ('2024-03-05', datetime.datetime(2024, 3, 5), True),  # Excel date cell
        ('2024-03-05T00:00:00', datetime.datetime(2024, 3, 5), True),
        ('2024-03-05', datetime.datetime(2024, 3, 5, 14, 30), False),
        ('2024-03-05T14:30:00', datetime.datetime(2024, 3, 5, 14, 30), True),
        ('14:30:00', datetime.time(14, 30), True),
        ('2024-03-06', datetime.date(2024, 3, 5), False),
    ],
)
def test_same_value(new, stored, same):
    assert same_value(new, stored) is same


# --- the real sheet -------------------------------------------------------------


def test_real_kit_sheet(real_kit_xlsx):
    catalog = build(real_kit_xlsx)
    speed, variation = 'C033', 'C007'
    result = ground(
        edit(
            target=speed,
            rows=Where(where=variation, equals='20min'),
            said='speed 4000 where variation is 20min',
        ),
        catalog,
    )
    assert result.rows == (6, 7, 8)  # samples 4-6, not the 20min-115°Ctsub ones
    result = ground(
        edit(
            target=speed,
            rows=between('20', '25'),
            said='speed 4000 for samples 20 to 25',
        ),
        catalog,
    )
    assert result.rows == (22, 23, 24, 25, 26)
    assert result.notes == ('samples 20 to 25: no sample 22, applied to 5 samples',)
    noop = ground(edit(target=speed, new='3000', said='speed 3000'), catalog)
    assert noop.status == 'noop'
