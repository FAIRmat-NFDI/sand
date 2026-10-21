import datetime

import pytest
from openpyxl.utils.cell import column_index_from_string, coordinate_from_string

from sand.sheet_edit.formulas import (
    ExcelError,
    Ref,
    Unsupported,
    as_text,
    evaluate,
    references,
    same_result,
)

# the two formulas in the KIT sheet
NOMAD_ID = '=CONCATENATE("KIT_",B3,"_",A3,"_",C3,"_",D3,"_",E3)'
DEPOSITION = '=IF(LEFT(G3,4)="left","",VALUE(_xlfn.TEXTBEFORE(G3,"min"))*60)'


def cells(values: dict[str, object]):
    """A cell getter from {'B3': 'JoDa', ...}; unknown cells are empty."""
    by_ref = {}
    for coord, value in values.items():
        col, row = coordinate_from_string(coord)
        by_ref[Ref(row, column_index_from_string(col))] = value
    return by_ref.get


def test_nomad_id_formula():
    row = {'A3': 20260914, 'B3': 'JoDa', 'C3': '275nmPbI2', 'D3': 0, 'E3': 1}
    assert evaluate(NOMAD_ID, cells(row)) == 'KIT_JoDa_20260914_275nmPbI2_0_1'


def test_numbers_join_as_excel_writes_them():
    # openpyxl reads 1 or 1.0; Excel writes both as "1"
    row = {'A3': 20260914.0, 'B3': 'x', 'C3': 'b', 'D3': 0.0, 'E3': 1.0}
    assert evaluate(NOMAD_ID, cells(row)) == 'KIT_x_20260914_b_0_1'


@pytest.mark.parametrize(
    ('variation', 'seconds'),
    [('15min', 900), ('45min-115°Ctsub', 2700), ('50 min', 3000), ('7.5min', 450)],
)
def test_deposition_time_formula(variation, seconds):
    assert evaluate(DEPOSITION, cells({'G3': variation})) == seconds


def test_deposition_time_left_branch_is_empty_and_case_insensitive():
    assert evaluate(DEPOSITION, cells({'G3': 'left out'})) == ''
    assert evaluate(DEPOSITION, cells({'G3': 'LEFT'})) == ''


def test_errors_are_raised_not_written():
    with pytest.raises(ExcelError, match='#N/A'):
        evaluate(DEPOSITION, cells({'G3': 'reference'}))
    with pytest.raises(ExcelError, match='#VALUE!'):
        evaluate(DEPOSITION, cells({'G3': 'fast min'}))
    with pytest.raises(ExcelError, match='#DIV/0!'):
        evaluate('=A1/B1', cells({'A1': 1}))


def test_references():
    assert references(NOMAD_ID) == {Ref(3, c) for c in (1, 2, 3, 4, 5)}
    assert references(DEPOSITION) == {Ref(3, 7)}
    assert references('=$A$1*2+b2') == {Ref(1, 1), Ref(2, 2)}


@pytest.mark.parametrize(
    ('formula', 'result'),
    [
        ('=A1+B1*2', 8),  # text "3" counts as a number
        ('=-A1^2', 4),  # Excel: unary minus binds before ^
        ('=(A1+1)&"x"', '3x'),
        ('=A1<>B1', True),  # number vs text: never equal
        ('=C1&"x"', 'x'),  # an empty cell is "" in text
        ('=C1+1', 1),  # and 0 in numbers
    ],
)
def test_operators_and_precedence(formula, result):
    assert evaluate(formula, cells({'A1': 2, 'B1': '3'})) == result


@pytest.mark.parametrize(
    'formula',
    [
        '=SUM(A1:A3)',  # range
        '=XLOOKUP(A1,B1,C1)',  # function not on the list
        '=Sheet2!A1',  # other sheet
        '=A1%',  # percent operator
        '=A1+',  # incomplete
    ],
)
def test_unsupported_formulas_are_refused(formula):
    with pytest.raises(Unsupported):
        evaluate(formula, cells({}))


def test_as_text_matches_excel_general_format():
    assert as_text(3000.0) == '3000'
    assert as_text(0.105) == '0.105'
    assert as_text(True) == 'TRUE'
    assert as_text(None) == ''


def test_same_result():
    assert same_result(900.0, 900)
    assert same_result('', None)
    assert not same_result('1', 1)
    assert not same_result(900.0, 901)


@pytest.mark.parametrize(
    ('formula', 'a1', 'result'),
    [
        ('=IF(A1>0,"t","f")', 'abc', 't'),  # Excel: text ranks above numbers
        ('=IF(A1<0,"t","f")', 'abc', 'f'),
        ('=A1=1', True, False),  # TRUE never equals 1
        ('=A1>"zzz"', True, True),  # TRUE ranks above text
        ('=A1=""', None, True),  # empty equals ""
        ('=A1=0', None, True),  # and 0
    ],
)
def test_mixed_type_comparison_follows_excel(formula, a1, result):
    assert evaluate(formula, cells({'A1': a1})) is result


def test_dates_are_unsupported_not_crashing():
    when = datetime.datetime(2025, 1, 1)
    for formula in ('=A1&"_"&B1', '=CONCATENATE(A1,B1)', '=B1+1', '=B1>0'):
        with pytest.raises(Unsupported, match='date'):
            evaluate(formula, cells({'A1': 'x', 'B1': when}))
