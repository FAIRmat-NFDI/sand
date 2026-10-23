"""Numbers and units in what the user said or typed, read exactly.

English only. A comma is a thousands separator ('4,000') or a list
('samples 1,3,7'), never a decimal point. Units are read, never converted:
'2 min' is not a value for a '[s]' column (conversion in code later, #108).
"""

import re
from dataclasses import dataclass
from decimal import Decimal

_ONES = {
    'zero': 0,
    'one': 1,
    'two': 2,
    'three': 3,
    'four': 4,
    'five': 5,
    'six': 6,
    'seven': 7,
    'eight': 8,
    'nine': 9,
    'ten': 10,
    'eleven': 11,
    'twelve': 12,
    'thirteen': 13,
    'fourteen': 14,
    'fifteen': 15,
    'sixteen': 16,
    'seventeen': 17,
    'eighteen': 18,
    'nineteen': 19,
}
_TENS = {
    'twenty': 20,
    'thirty': 30,
    'forty': 40,
    'fifty': 50,
    'sixty': 60,
    'seventy': 70,
    'eighty': 80,
    'ninety': 90,
}
_SCALES = {'thousand': 1000, 'million': 1_000_000}
_HUNDRED = 100
_DIGIT_WORD = 10  # ones words below this are single digits ('point two five')

# Canonical unit -> spellings. Symbols match case-sensitively ('mM' is
# millimolar, 'mm' millimetre); spelled-out words match in any case.
_UNITS = {
    's': (['s'], ['sec', 'secs', 'second', 'seconds']),
    'min': (['min'], ['mins', 'minute', 'minutes']),
    'h': (['h'], ['hr', 'hrs', 'hour', 'hours']),
    'rpm/s': (['rpm/s'], ['rpm per second']),
    'rpm': (['rpm'], ['rpms']),
    'krpm': (['krpm'], []),
    '°C': (
        ['°C', '° C', 'C'],
        ['degc', 'celsius', 'degrees celsius', 'degree', 'degrees'],
    ),
    'K': ([], ['kelvin']),
    'uL': (
        ['uL', 'µL', 'μL', 'ul', 'µl', 'μl'],
        ['microliter', 'microliters', 'microlitre', 'microlitres'],
    ),
    'mL': (['mL', 'ml'], ['milliliter', 'milliliters', 'millilitre', 'millilitres']),
    'mM': (['mM'], ['millimolar']),
    'M': (['M'], ['molar']),
    'nm': (['nm'], ['nanometer', 'nanometers', 'nanometre', 'nanometres']),
    'mm': (['mm'], ['millimeter', 'millimeters', 'millimetre', 'millimetres']),
    'angstrom/s': (
        ['angstrom/s', 'Å/s'],
        ['angstrom per second', 'angstroms per second'],
    ),
    '%': (['%'], ['percent', 'per cent']),
    'mbar': (['mbar'], ['millibar', 'millibars']),
    'bar': (['bar'], ['bars']),
    'ppm': (['ppm'], []),
    'cm^2': (['cm^2', 'cm²'], ['square centimeter', 'square centimeters']),
}
_SPELLINGS = sorted(
    [(sym, unit, False) for unit, (syms, _) in _UNITS.items() for sym in syms]
    + [(word, unit, True) for unit, (_, words) in _UNITS.items() for word in words],
    key=lambda s: -len(s[0]),
)
_SYMBOLS = {sym: unit for unit, (syms, _) in _UNITS.items() for sym in syms}
_WORDS = {word: unit for unit, (_, words) in _UNITS.items() for word in words}

_DIGITS_RE = re.compile(
    r'(?<![\w.)])'  # not after a word or ')' : the 3 in 'Pb(I0.83Br0.17)3'
    r'(?:(?<![\w.-])-)?'
    r'(?:\d{1,3}(?:,\d{3})+|\d+)?(?:\.\d+)?'
)
_WORD_RE = re.compile(r'[A-Za-z]+')
_RANGE_WORDS = {'-', '–', '—', 'to', 'through', 'thru'}


@dataclass(frozen=True)
class Number:
    text: str  # as written, with its unit: 'four thousand rpm', '20min'
    value: Decimal
    unit: str | None  # canonical: 'seconds' -> 's'
    start: int
    end: int


@dataclass(frozen=True)
class Range:
    first: Number
    last: Number


def numbers(text: str) -> list[Number]:
    """Every number, in order. A digit inside a word is not one: the 2 in
    'PbI2', 'C033', '2nd', 'Cs0.05'."""
    return sorted([*_digit_numbers(text), *_word_numbers(text)], key=lambda n: n.start)


def ranges(text: str) -> list[Range]:
    """'1 to 3', '1-3', '1–3', 'one through three'."""
    found = numbers(text)
    return [
        Range(a, b)
        for a, b in zip(found, found[1:])
        if text[a.end : b.start].strip().lower() in _RANGE_WORDS
    ]


def value(text: str) -> Number | None:
    """The number when the whole text is one ('4000', 'four thousand',
    '4000 rpm'); otherwise None."""
    found = numbers(text)
    stripped = text.strip()
    lead = len(text) - len(text.lstrip())
    if len(found) == 1 and (found[0].start, found[0].end) == (
        lead,
        lead + len(stripped),
    ):
        return found[0]
    return None


class UnitMismatch(Exception):
    """The value's unit is not the column's; nothing is converted (#108)."""


def check_unit(number: Number, header_unit: str | None) -> None:
    """Raise unless the number's unit, if any, is a spelling of the
    header's unit: '30 sec' fits '[s]', '2 min' does not."""
    if number.unit is None or same_unit(number.unit, header_unit or ''):
        return
    where = f'a [{header_unit}] column' if header_unit else 'a column without a unit'
    raise UnitMismatch(f'{number.text!r} is in {number.unit}, not {where}')


def same_unit(said: str, header: str) -> bool:
    """Whether `said` is a spelling of the header's unit; never converts."""
    return _canonical(said) == _canonical(header)


def _canonical(unit: str) -> str:
    unit = unit.strip()
    return _SYMBOLS.get(unit) or _WORDS.get(unit.lower(), unit)


def _in_word(ch: str) -> bool:
    return ch.isalnum() or ch in '_°µμ'


# --- digits ---------------------------------------------------------------


def _digit_numbers(text: str) -> list[Number]:
    out = []
    for m in _DIGITS_RE.finditer(text):
        written = m.group()
        if not any(ch.isdigit() for ch in written):
            continue
        end, number = m.end(), Decimal(written.replace(',', ''))
        scale = re.match(r'\s+(hundred|thousand|million)\b', text[end:], re.IGNORECASE)
        if scale:  # '4 thousand'
            word = scale.group(1).lower()
            number *= _HUNDRED if word == 'hundred' else _SCALES[word]
            end += scale.end()
        glued = _unit_at(text, end, glued=True)
        if glued:
            unit, end = glued
        elif end < len(text) and _in_word(text[end]):
            continue  # '2nd', '4PACz', '115°Ctsub': part of a word
        else:
            spaced = _unit_at(text, end, glued=False)
            unit, end = spaced if spaced else (None, end)
        out.append(Number(text[m.start() : end], number, unit, m.start(), end))
    return out


def _unit_at(text: str, pos: int, glued: bool) -> tuple[str, int] | None:
    """A unit spelling starting at `pos` (after spaces unless glued) and
    ending at a word boundary: (canonical unit, end)."""
    start = pos if glued else pos + len(text[pos:]) - len(text[pos:].lstrip(' '))
    if not glued and start == pos:
        return None
    for spelling, unit, any_case in _SPELLINGS:
        piece = text[start : start + len(spelling)]
        if (piece.lower() == spelling.lower()) if any_case else (piece == spelling):
            end = start + len(spelling)
            if end == len(text) or not _in_word(text[end]):
                return unit, end
    return None


# --- words ----------------------------------------------------------------


def _word_numbers(text: str) -> list[Number]:
    words = list(_WORD_RE.finditer(text))
    out, i = [], 0
    while i < len(words):
        read = _read_words(text, words, i)
        if read is None:
            i += 1
            continue
        j, number = read
        start, end = words[i].start(), words[j - 1].end()
        if not _inside_word(text, start, end):
            unit = _unit_at(text, end, glued=False)
            if unit:
                end = unit[1]
            out.append(Number(text[start:end], number, unit and unit[0], start, end))
        i = j
    return out


def _inside_word(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start else ' '
    after = text[end] if end < len(text) else ' '
    return before.isdigit() or after.isdigit()


def _read_words(text: str, words: list, i: int) -> tuple[int, Decimal] | None:  # noqa: PLR0912
    """One spoken number from words[i]: (index after it, value) or None.
    'two three' is two numbers; 'twenty five' and 'fifteen hundred' are one."""

    def word(k: int) -> str:
        return words[k].group().lower() if k < len(words) else ''

    def joined(k: int) -> bool:  # words[k] follows words[k-1] with only a space or '-'
        return text[words[k - 1].end() : words[k].start()].strip() in ('', '-')

    k, sign = i, 1
    if word(k) in ('minus', 'negative') and k + 1 < len(words) and joined(k + 1):
        sign, k = -1, k + 1
    total, current, last, used = 0, None, None, False
    decimals = ''
    while k < len(words) and (k == i or joined(k)):
        w = word(k)
        if w == 'a' and current is None and word(k + 1) in ('hundred', *_SCALES):
            current, last = 1, 'ones'
        elif w in _ONES:
            n = _ONES[w]
            if last in ('ones', 'teen') or (
                last == 'tens' and (n >= _DIGIT_WORD or n == 0)
            ):
                break
            current, last = (current or 0) + n, 'teen' if n >= _DIGIT_WORD else 'ones'
            used = True
        elif w in _TENS:
            if last in ('ones', 'teen', 'tens'):
                break
            current, last, used = (current or 0) + _TENS[w], 'tens', True
        elif w == 'hundred' and current is not None and last != 'hundred':
            current, last, used = current * _HUNDRED, 'hundred', True
        elif w in _SCALES and current is not None:
            total, current, last = total + current * _SCALES[w], None, 'scale'
            used = True
        elif (
            w == 'and'
            and last in ('hundred', 'scale')
            and word(k + 1) in {**_ONES, **_TENS}
        ):
            pass
        elif w == 'point' and used:
            k += 1
            while (
                k < len(words)
                and joined(k)
                and _ONES.get(word(k), _DIGIT_WORD) < _DIGIT_WORD
            ):
                decimals += str(_ONES[word(k)])
                k += 1
            if not decimals:
                k -= 1
            break
        else:
            break
        k += 1
    if not used:
        return None
    number = Decimal(total + (current or 0))
    if decimals:
        number += Decimal(f'0.{decimals}')
    return k, sign * number
