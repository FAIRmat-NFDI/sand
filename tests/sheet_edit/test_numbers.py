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
        ('50 µl', [('50', 'µl')]),
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
        ('x^2', []),  # exponents
        ('cm^2', []),
        ('x ^ 2', []),
        ('5 cm^2', [('5', 'cm^2')]),
        # a whole instruction
        (
            'spin coating 2 speed four thousand rpm for samples 1-3',
            [('2', None), ('4000', 'rpm'), ('1', None), ('3', None)],
        ),
    ],
)
def test_numbers(text, expected):
    assert found(text) == expected


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        # spellings pint knows, glued or spaced, words in any case
        ('30s', [('30', 's')]),
        ('30 s', [('30', 's')]),
        ('30 SECONDS', [('30', 's')]),
        ('30 Seconds', [('30', 's')]),
        ('30 secs', [('30', 's')]),
        ('5 Min', [('5', 'min')]),
        ('3 MBAR', [('3', 'mbar')]),
        ('50 μl', [('50', 'µl')]),  # Greek mu and micro sign
        ('50 µL', [('50', 'µl')]),
        ('50 microlitres', [('50', 'µl')]),
        ('300 K', [('300', 'K')]),
        ('300K', [('300', 'K')]),
        ('4 krpm', [('4', 'krpm')]),
        ('0.1 cm²', [('0.1', 'cm^2')]),
        ('2 angstrom/s', [('2', 'Å/s')]),
        # lab speech pint reads differently
        ('100 °C', [('100', '°C')]),
        ('100 ° C', [('100', '°C')]),
        ('100 C', [('100', '°C')]),
        ('100 degC', [('100', '°C')]),
        ('100 deg C', [('100', '°C')]),
        ('100 degrees', [('100', '°C')]),
        ('100 celsius', [('100', '°C')]),
        ('5 rpm per second', [('5', 'rpm/s')]),
        ('2 angstroms per second', [('2', 'Å/s')]),
        ('0.1 square centimeters', [('0.1', 'cm^2')]),
        ('35 per cent', [('35', '%')]),
        # the unit ends at a word boundary
        ('30 sec.', [('30', 's')]),
        ('(30 sec)', [('30', 's')]),
        ('30 sec, then', [('30', 's')]),
        # symbols keep their case: mm is not mM; S (siemens) is not s
        ('50 mm', [('50', 'mm')]),
        ('50 mM', [('50', 'mM')]),
        ('30 S', [('30', None)]),
        # English words and single letters pint knows are not units here
        ('4000 at sample 1', [('4000', None), ('1', None)]),
        ('3 in spin coating', [('3', None)]),
        ('2 a', [('2', None)]),
        ('2 g', [('2', None)]),
        ('1 to 3', [('1', None), ('3', None)]),
        (
            'set 4000 for sample 1 then 3 samples at 5 in step 2',
            [('4000', None), ('1', None), ('3', None), ('5', None), ('2', None)],
        ),
    ],
)
def test_units_after_a_number(text, expected):
    assert found(text) == expected


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('one', ['1']),
        ('twenty one', ['21']),
        ('forty-five', ['45']),
        ('one hundred', ['100']),
        ('one hundred twenty', ['120']),
        ('twelve hundred', ['1200']),
        ('one thousand two hundred thirty four', ['1234']),
        ('two thousand and five', ['2005']),
        ('two thousand five hundred', ['2500']),
        ('a hundred and five', ['105']),
        ('a thousand', ['1000']),
        ('one million', ['1000000']),
        ('Four Thousand', ['4000']),
        ('one point two five', ['1.25']),
        ('two point zero', ['2.0']),
        ('zero point one', ['0.1']),
        ('three point', ['3']),  # nothing after 'point'
        ('negative five', ['-5']),
        # two numbers, not one
        ('one one', ['1', '1']),
        ('ten twenty', ['10', '20']),
        ('twenty ten', ['20', '10']),
        ('nineteen eighty', ['19', '80']),
        ('one and two', ['1', '2']),
        ('two and a half', ['2']),
        # not numbers
        ('hundred', []),
        ('thousand', []),
        ('point five', ['0.5']),
        ('minus', []),
        ('a', []),
        ('someone', []),
        ('tone', []),
        ('2one', []),  # glued to a digit: neither is a number
    ],
)
def test_spoken_numbers(text, expected):
    assert [str(n.value) for n in numbers(text)] == expected


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
    ('text', 'unit'),
    [
        ('1-3s', 's'),
        ('1s - 3s', 's'),
        ('from 1 second to 3', 's'),
        ('1 to 3 seconds', 's'),
        ('1 to 3', None),
    ],
)
def test_a_range_has_one_unit(text, unit):
    (r,) = ranges(text)
    assert r.unit == unit


def test_a_range_with_two_units_is_refused():
    (r,) = ranges('1 s to 3 min')
    with pytest.raises(UnitMismatch, match='two units'):
        r.unit


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
        ('kelvin', 'K', True),
        ('cm²', 'cm^2', True),
        ('a.u.', 'a.u.', True),  # a unit pint does not know: by spelling
        ('a.u.', 'arb', False),
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
        ('300 K', '°C', "'300 K' is in K, not a [°C] column"),
    ],
)
def test_check_unit_refuses_a_different_unit(text, header, message):
    with pytest.raises(UnitMismatch, match=message.replace('[', r'\[')):
        check_unit(value(text), header)
