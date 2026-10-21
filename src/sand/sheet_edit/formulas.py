"""A small Excel formula calculator for the formulas lab sheets use.

Nothing on the server can recalculate Excel formulas, and NOMAD's parser
reads only the result Excel stored for each formula. So when an edit
changes a formula's input, sand computes the new result itself. Only a
whitelist of functions is supported; anything else raises Unsupported and
the edit is refused. Callers must prove the calculator on each file first:
it has to reproduce the result Excel stored (write._new_result does).
"""

import datetime
import re
from collections.abc import Callable
from dataclasses import dataclass

from openpyxl.utils import column_index_from_string

Value = float | str | bool | None  # None: empty cell

# Excel writes integers up to 15 digits without a decimal point
_MAX_PLAIN_INTEGER = 1e15


class Unsupported(Exception):
    """Formula syntax or function sand cannot compute."""


class ExcelError(Exception):
    """The formula's result is an Excel error (#VALUE!, #N/A, ...)."""


_TOKEN_RE = re.compile(
    r'\s*(?:'
    r'(?P<string>"(?:[^"]|"")*")'
    r'|(?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)'
    r'|(?P<func>(?:_xlfn\.)?[A-Za-z][A-Za-z0-9.]*)\('
    r'|(?P<ref>\$?[A-Za-z]{1,3}\$?\d+)(?![A-Za-z0-9_(!:])'
    r'|(?P<op><>|<=|>=|[-+*/^&=<>(),])'
    r')'
)


def _tokens(formula: str) -> list[tuple[str, str]]:
    text = formula[1:] if formula.startswith('=') else formula
    out, pos = [], 0
    while pos < len(text):
        if text[pos:].strip() == '':
            break
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise Unsupported(f'cannot read {text[pos:]!r}')
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


# --- parsing: tokens -> tree ------------------------------------------------
# Excel precedence, low to high: comparison, &, + -, * /, ^, unary minus.

_LEVELS = [('=', '<>', '<', '>', '<=', '>='), ('&',), ('+', '-'), ('*', '/'), ('^',)]


@dataclass(frozen=True)
class Ref:
    row: int
    column: int


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens, self.i = tokens, 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise Unsupported('formula ends too early')
        self.i += 1
        return tok

    def expect(self, op: str) -> None:
        if self.take() != ('op', op):
            raise Unsupported(f'expected {op!r}')

    def parse(self):
        tree = self.binary(0)
        if self.peek() is not None:
            raise Unsupported(f'unexpected {self.peek()[1]!r}')
        return tree

    def binary(self, level: int):
        if level == len(_LEVELS):
            return self.unary()
        left = self.binary(level + 1)
        while (tok := self.peek()) and tok[0] == 'op' and tok[1] in _LEVELS[level]:
            self.take()
            left = ('op', tok[1], left, self.binary(level + 1))
        return left

    def unary(self):
        tok = self.peek()
        if tok == ('op', '-'):
            self.take()
            return ('neg', self.unary())
        if tok == ('op', '+'):
            self.take()
            return self.unary()
        return self.atom()

    def atom(self):
        kind, text = self.take()
        if kind == 'number':
            return ('const', float(text))
        if kind == 'string':
            return ('const', text[1:-1].replace('""', '"'))
        if kind == 'ref':
            m = re.match(r'\$?([A-Za-z]+)\$?(\d+)', text)
            return (
                'ref',
                Ref(int(m.group(2)), column_index_from_string(m.group(1).upper())),
            )
        if kind == 'func':
            name = text.upper().removeprefix('_XLFN.')
            if name not in _FUNCTIONS and name != 'IF':
                raise Unsupported(f'function {name} is not supported')
            args = []
            if self.peek() != ('op', ')'):
                args.append(self.binary(0))
                while self.peek() == ('op', ','):
                    self.take()
                    args.append(self.binary(0))
            self.expect(')')
            return ('call', name, args)
        if (kind, text) == ('op', '('):
            inner = self.binary(0)
            self.expect(')')
            return inner
        raise Unsupported(f'unexpected {text!r}')


def parse(formula: str):
    return _Parser(_tokens(formula)).parse()


def references(formula: str) -> set[Ref]:
    """Cells the formula reads (same sheet only)."""
    found: set[Ref] = set()

    def walk(node) -> None:
        if node[0] == 'ref':
            found.add(node[1])
        elif node[0] == 'neg':
            walk(node[1])
        elif node[0] == 'op':
            walk(node[2])
            walk(node[3])
        elif node[0] == 'call':
            for arg in node[2]:
                walk(arg)

    walk(parse(formula))
    return found


# --- Excel value semantics ---------------------------------------------------


_DATES = (datetime.date, datetime.time, datetime.timedelta)


def _no_dates(v) -> None:
    # Excel stores dates as serial numbers shown through a format; sand
    # does not reproduce that, so formulas over dates are refused
    if isinstance(v, _DATES):
        raise Unsupported(f'date values are not supported ({v})')


def as_text(v: Value) -> str:
    """How Excel turns a value into text (General format)."""
    _no_dates(v)
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'TRUE' if v else 'FALSE'
    if isinstance(v, int | float):
        f = float(v)
        return (
            str(int(f))
            if f.is_integer() and abs(f) < _MAX_PLAIN_INTEGER
            else format(f, '.15g')
        )
    return v


def as_number(v: Value) -> float:
    _no_dates(v)
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, int | float):
        return float(v)
    try:
        return float(v.strip())
    except ValueError:
        raise ExcelError(f'#VALUE! ({v!r} is not a number)') from None


def _as_bool(v: Value) -> bool:
    if isinstance(v, str):
        if v.upper() in ('TRUE', 'FALSE'):
            return v.upper() == 'TRUE'
        raise ExcelError(f'#VALUE! ({v!r} is not TRUE/FALSE)')
    return bool(as_number(v))


# Excel orders values of different types: numbers < text < TRUE/FALSE
def _rank(v: Value) -> int:
    if isinstance(v, bool):
        return 2
    return 1 if isinstance(v, str) else 0


def _compare(op: str, a: Value, b: Value) -> bool:
    """Excel's comparison: text case-insensitively, different types by
    rank (so "abc" > 0 is TRUE, and TRUE never equals 1)."""
    _no_dates(a)
    _no_dates(b)
    # an empty cell takes the other side's type
    if a is None:
        a = '' if isinstance(b, str) else False if isinstance(b, bool) else 0.0
    if b is None:
        b = '' if isinstance(a, str) else False if isinstance(a, bool) else 0.0
    if _rank(a) != _rank(b):
        x, y = _rank(a), _rank(b)
    elif isinstance(a, str):
        x, y = a.casefold(), b.casefold()
    else:
        x, y = float(a), float(b)
    return {
        '=': x == y,
        '<>': x != y,
        '<': x < y,
        '>': x > y,
        '<=': x <= y,
        '>=': x >= y,
    }[op]


def _left(text, n=1.0):
    return as_text(text)[: int(as_number(n))]


def _right(text, n=1.0):
    n = int(as_number(n))
    return as_text(text)[-n:] if n else ''


def _textbefore(text, delimiter):
    text, delimiter = as_text(text), as_text(delimiter)
    if delimiter not in text:
        raise ExcelError(f'#N/A ({delimiter!r} not in {text!r})')
    return text.split(delimiter, 1)[0]


def _textafter(text, delimiter):
    text, delimiter = as_text(text), as_text(delimiter)
    if delimiter not in text:
        raise ExcelError(f'#N/A ({delimiter!r} not in {text!r})')
    return text.split(delimiter, 1)[1]


_FUNCTIONS: dict[str, Callable] = {
    'CONCATENATE': lambda *a: ''.join(as_text(x) for x in a),
    'CONCAT': lambda *a: ''.join(as_text(x) for x in a),
    'LEFT': _left,
    'RIGHT': _right,
    'VALUE': as_number,
    'TEXTBEFORE': _textbefore,
    'TEXTAFTER': _textafter,
    'TRIM': lambda v: ' '.join(as_text(v).split()),
    'UPPER': lambda v: as_text(v).upper(),
    'LOWER': lambda v: as_text(v).lower(),
}

_ARITHMETIC = {
    '+': lambda a, b: a + b,
    '-': lambda a, b: a - b,
    '*': lambda a, b: a * b,
    '/': lambda a, b: a / b,
    '^': lambda a, b: a**b,
}


def evaluate(formula: str, cell: Callable[[Ref], Value]) -> Value:
    """The formula's result; `cell(ref)` gives each referenced cell's value."""
    return _eval(parse(formula), cell)


def _eval(node, cell: Callable[[Ref], Value]) -> Value:
    kind = node[0]
    if kind == 'const':
        return node[1]
    if kind == 'ref':
        return cell(node[1])
    if kind == 'neg':
        return -as_number(_eval(node[1], cell))
    if kind == 'call':
        return _call(node[1], node[2], cell)
    return _binary(node[1], _eval(node[2], cell), _eval(node[3], cell))


def _call(name: str, args: list, cell: Callable[[Ref], Value]) -> Value:
    if name == 'IF':  # evaluates only the branch it takes, as Excel does
        if len(args) not in (2, 3):  # noqa: PLR2004
            raise Unsupported('IF needs 2 or 3 arguments')
        if _as_bool(_eval(args[0], cell)):
            return _eval(args[1], cell)
        return _eval(args[2], cell) if len(args) == 3 else False  # noqa: PLR2004
    try:
        return _FUNCTIONS[name](*(_eval(a, cell) for a in args))
    except TypeError as exc:
        raise Unsupported(f'{name}: wrong number of arguments') from exc


def _binary(op: str, a: Value, b: Value) -> Value:
    if op == '&':
        return as_text(a) + as_text(b)
    if op in _ARITHMETIC:
        try:
            return _ARITHMETIC[op](as_number(a), as_number(b))
        except ZeroDivisionError:
            raise ExcelError('#DIV/0!') from None
    return _compare(op, a, b)


def same_result(computed: Value, stored: Value) -> bool:
    """Whether a computed result equals the one Excel stored."""
    if computed in ('', None) and stored in ('', None):
        return True
    if isinstance(computed, bool) or isinstance(stored, bool):
        return computed == stored
    if isinstance(computed, int | float) and isinstance(stored, int | float):
        return abs(computed - stored) <= 1e-9 * max(1.0, abs(stored))
    return computed == stored
