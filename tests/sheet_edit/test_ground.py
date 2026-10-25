import pytest
from synthetic_sheet import edited, letter, with_formula_results

from sand.sheet_edit.catalog import build
from sand.sheet_edit.edits import Edit, Range, Samples, Where
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


def edit(
    target=SPEED, rows='all', old='3000', new='4000', said='spin coating 2 speed 4000'
):
    return Edit(target=target, rows=rows, anchor=None, old=old, new=new, said=said)


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


def test_a_subbatch_the_edit_names_must_have_been_said(two_subbatches):
    why = refused(
        ground(
            edit(rows=samples('1', subbatch='1'), said='speed 4000 for sample 1'),
            two_subbatches,
        )
    )
    assert why == 'you did not name subbatch 1'


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


def test_a_range_the_user_did_not_name_is_refused(catalog):
    why = refused(
        ground(
            edit(rows=between('1', '3'), said='speed 4000 for the first three samples'),
            catalog,
        )
    )
    assert why == 'samples 1 to 3: you did not name them'


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
        ('samples 1 to 3', ()),
        ('samples with variation 15min', ()),
    ],
)
def test_said_samples_unknown(catalog, said, unknown):
    assert said_samples(said, catalog).unknown == unknown


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
        edit(target=SUBSTRATE, new='120', said='substrate temperature 120'), catalog
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
