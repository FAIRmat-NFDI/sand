from decimal import Decimal

import pytest

from sand.sheet_edit.numbers import (
    UnitMismatch,
    check_unit,
    numbers,
    ranges,
    same_unit,
    value,
)


def found(text: str) -> list[tuple[str, str | None]]:
    return [(str(n.value), n.unit) for n in numbers(text)]


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        # digits
        ('4000', [('4000', None)]),
        ('4,000', [('4000', None)]),
        ('3.5', [('3.5', None)]),
        ('.5', [('0.5', None)]),
        ('-5', [('-5', None)]),
        ('4000.', [('4000', None)]),
        ('(4000)', [('4000', None)]),
        ('0.1', [('0.1', None)]),
        # a comma is a list, never a decimal point
        ('samples 1,3,7', [('1', None), ('3', None), ('7', None)]),
        ('3,5', [('3', None), ('5', None)]),
        # words
        ('four thousand', [('4000', None)]),
        ('fifteen hundred', [('1500', None)]),
        ('twenty-five', [('25', None)]),
        ('twenty five', [('25', None)]),
        ('one hundred and twenty', [('120', None)]),
        ('a hundred', [('100', None)]),
        ('4 thousand', [('4000', None)]),
        ('three point two five', [('3.25', None)]),
        ('minus five', [('-5', None)]),
        ('two three', [('2', None), ('3', None)]),  # two numbers
        ('zero', [('0', None)]),
        # units, spaced or glued
        ('4000 rpm', [('4000', 'rpm')]),
        ('20min', [('20', 'min')]),
        ('100°C', [('100', '°C')]),
        ('100 C', [('100', '°C')]),
        ('100 degrees celsius', [('100', '°C')]),
        ('35%', [('35', '%')]),
        ('50 µl', [('50', 'uL')]),
        ('30 seconds', [('30', 's')]),
        ('2 h', [('2', 'h')]),
        ('four thousand rpm', [('4000', 'rpm')]),
        # not numbers: digits inside names and formulas
        ('PbI2', []),
        ('C033', []),
        ('box1', []),
        ('2nd', []),
        ('Me-4PACz', []),
        ('Cs0.05(MA0.17FA0.83)0.95Pb(I0.83Br0.17)3', []),
        ('20min-115°Ctsub', [('20', 'min')]),  # the label's own number
        # a whole instruction
        (
            'spin coating 2 speed four thousand rpm for samples 1-3',
            [('2', None), ('4000', 'rpm'), ('1', None), ('3', None)],
        ),
    ],
)
def test_numbers(text, expected):
    assert found(text) == expected


def test_numbers_keep_their_text_and_position():
    (n,) = numbers('set speed to four thousand rpm now')
    assert (n.text, n.start, n.end) == ('four thousand rpm', 13, 30)
    assert n.value == Decimal(4000)


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('samples 1 to 3', [('1', '3')]),
        ('1-3', [('1', '3')]),
        ('1–3', [('1', '3')]),
        ('10 through 12', [('10', '12')]),
        ('one to three', [('1', '3')]),
        ('samples 1 and 3', []),
        ('1, 3', []),
    ],
)
def test_ranges(text, expected):
    assert [(str(r.first.value), str(r.last.value)) for r in ranges(text)] == expected


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('4000', ('4000', None)),
        (' four thousand ', ('4000', None)),
        ('4000 rpm', ('4000', 'rpm')),
        ('4000 rpm now', None),
        ('20min-115°Ctsub', None),
        ('Ethanol', None),
        ('', None),
    ],
)
def test_value_is_the_whole_text_or_nothing(text, expected):
    n = value(text)
    assert (n and (str(n.value), n.unit)) == expected


@pytest.mark.parametrize(
    ('said', 'header', 'same'),
    [
        ('sec', 's', True),
        ('seconds', 's', True),
        ('SECONDS', 's', True),
        ('degrees', '°C', True),
        ('µl', 'uL', True),
        ('min', 's', False),  # never converted
        ('krpm', 'rpm', False),
        ('mm', 'mM', False),  # millimetre is not millimolar
        ('MM', 'mM', False),
    ],
)
def test_same_unit(said, header, same):
    assert same_unit(said, header) is same


@pytest.mark.parametrize(
    ('text', 'header'),
    [('30 sec', 's'), ('4000', 'rpm'), ('4000', None), ('100 degrees', '°C')],
)
def test_check_unit_accepts_no_unit_or_the_same_unit(text, header):
    check_unit(value(text), header)


@pytest.mark.parametrize(
    ('text', 'header', 'message'),
    [
        ('2 min', 's', "'2 min' is in min, not a [s] column"),
        ('4 krpm', 'rpm', "'4 krpm' is in krpm, not a [rpm] column"),
        ('5 rpm', None, "'5 rpm' is in rpm, not a column without a unit"),
    ],
)
def test_check_unit_refuses_a_different_unit(text, header, message):
    with pytest.raises(UnitMismatch, match=message.replace('[', r'\[')):
        check_unit(value(text), header)
