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
        # thousands separators and decimals
        ('4,000', ['4000']),
        ('4,000,000', ['4000000']),
        ('12,345,678', ['12345678']),
        ('4,000.5', ['4000.5']),
        ('4000.5', ['4000.5']),
        ('0.1', ['0.1']),
        ('00.1', ['0.1']),
        ('007', ['7']),
        ('.5', ['0.5']),
        # signs: only a '-' glued to the number is a sign
        ('-5', ['-5']),
        ('-0.5', ['-0.5']),
        ('- 5', ['5']),
        ('3-5', ['3', '5']),
        # digits with a scale word, and what follows the scale
        ('4 thousand', ['4000']),
        ('4 Thousand', ['4000']),
        ('4 hundred', ['400']),
        ('4 million', ['4000000']),
        ('4 million and 23', ['4000023']),
        ('4 million and twenty three', ['4000023']),
        ('4 million 23', ['4000023']),
        ('4 thousand and 5', ['4005']),
        ('4 thousand five hundred', ['4500']),
        ('2 hundred and 5', ['205']),
        ('4 thousand and 5000', ['4000', '5000']),  # 5000 is not below a thousand
        ('4 thousand and 2.5', ['4000', '2.5']),
        ('4 million 500 thousand', ['4000000', '500000']),  # not joined: two numbers
        ('4 thousand and 5 samples', ['4005']),
        ('4thousand', []),
        # lists and punctuation around a number
        ('4,5,6', ['4', '5', '6']),
        ('1 2 3', ['1', '2', '3']),
        ('4000,', ['4000']),
        ('4000;', ['4000']),
        ('(4000)', ['4000']),
        ('[4000]', ['4000']),
        ('4000/5', ['4000', '5']),
        ('4000.', ['4000']),
        ('1.', ['1']),
        ('1...3', ['1', '3']),
        ('1.2.3', ['1.2']),  # a version, not numbers; the rest is dropped
        # speech transcribed with 'point' or 'minus'
        ('3 point 5', ['3.5']),
        ('3 point 50', ['3.50']),
        ('-3 point 5', ['-3.5']),
        ('minus 5', ['-5']),
        ('negative 5', ['-5']),
        ('minus 5.5', ['-5.5']),
        ('−5', ['-5']),  # Unicode minus
        ('plus 5', ['5']),
        # scientific notation, as pressures are written
        ('1e-6', ['0.000001']),
        ('1E-6', ['0.000001']),
        ('5e3', ['5000']),
        ('1x10^-6', ['0.000001']),
        ('1×10^-6', ['0.000001']),
        ('1*10^-6', ['0.000001']),
        ('2 x 10^-6', ['0.000002']),
        ('2.5 × 10^3', ['2500']),
        ('10^-6', ['0.000001']),
        ('4.000', ['4']),
        # digits inside a word
        ('1e5x', []),
        ('2x', []),
        ('v2', []),
        ('v2.1', []),
        ('a2', []),
        ('2 a 3', ['2', '3']),
        ('2026-09-14', ['2026', '9', '14']),  # a date is three numbers, not a sum
    ],
)
def test_digit_numbers(text, expected):
    assert [str(n.value) for n in numbers(text)] == expected


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('1e-6 bar', [('0.000001', 'bar')]),
        ('10^-6 bar', [('0.000001', 'bar')]),
        ('1 x 10^-6 bar', [('0.000001', 'bar')]),
        ('minus 20 degrees', [('-20', '°C')]),
        ('−5 °C', [('-5', '°C')]),
        ('2 hundred and 5 rpm', [('205', 'rpm')]),
    ],
)
def test_digit_forms_keep_their_unit(text, expected):
    assert found(text) == expected


@pytest.mark.parametrize(
    'text',
    [
        'sixthing hundred',  # a misheard or misspelt number word
        'fourty',
        'fourthousand',
        'a dozen',
        'half',
        'quarter',
        'first',
        'the second sample',
    ],
)
def test_unknown_words_are_never_guessed(text):
    assert numbers(text) == []


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        # read as far as the words allow; the rest is left, never guessed
        ('four, thousand', ['4']),
        ('hundred and five', ['5']),
        ('one oh five', ['1', '5']),
        ('one and a half', ['1']),
        ('4 000', ['4', '0']),  # a space is not a thousands separator
        ('1/2', ['1', '2']),
        ('1:30', ['1', '30']),
    ],
)
def test_forms_not_understood_read_only_what_is_sure(text, expected):
    assert [str(n.value) for n in numbers(text)] == expected


def test_a_scale_and_its_rest_are_one_number_with_one_unit():
    (n,) = numbers('spin at 4 thousand and 5 rpm please')
    assert (n.text, str(n.value), n.unit, n.start, n.end) == (
        '4 thousand and 5 rpm',
        '4005',
        'rpm',
        8,
        28,
    )


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
        ('2 A/s', [('2', 'Å/s')]),  # never ampere per second in a lab sheet
        ('1 cm2', [('1', 'cm^2')]),
        ('30 s 2', [('30', 's'), ('2', None)]),  # 's2' only when glued
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
        ('1 Å', [('1', 'Å')]),
        ('5 L', [('5', None)]),  # not a lab unit by one letter
        # temperature as typed: ordinal indicator for °, ℃ as one character
        ('100ºC', [('100', '°C')]),
        ('100 ºC', [('100', '°C')]),
        ('100℃', [('100', '°C')]),
        ('100 °c', [('100', '°C')]),
        ('4K', [('4', 'K')]),
        ('4k', []),
        # pint would multiply letters it knows ('%RH' = percent * R * H)
        ('60 %RH', [('60', None)]),
        ('5 mol%', [('5', None)]),
        ('5 at%', [('5', None)]),
        ('5 wt%', [('5', None)]),
        ('5 rpm per sample', [('5', 'rpm')]),
        ('5 rpm per s', [('5', 'rpm/s')]),
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
        ('1..3', [('1', '3')]),
        ('1...3', [('1', '3')]),
        ('1 till 3', [('1', '3')]),
        ('1 until 3', [('1', '3')]),
        ('between 1 and 3', []),  # could be a list of two: left to the LLM
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
