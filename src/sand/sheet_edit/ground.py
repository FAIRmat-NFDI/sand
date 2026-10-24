"""Code checks every proposal against the real sheet before anything is
written.

Part 1, structure: W the target is writable, R the rows exist and are
found by exact match, S the samples the user named are the ones used,
N rows that already hold the value are reported, not written. Each
failure is a refusal with a reason; nothing is guessed or repaired.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from sand.sheet_edit.catalog import Catalog, ColumnInfo, SampleInfo, sample_ranges
from sand.sheet_edit.edits import Edit, Range, Samples, Where
from sand.sheet_edit.numbers import numbers, ranges, value

_SAMPLE_WORD_RE = re.compile(r'\bsamples?\b', re.IGNORECASE)
_SUBBATCH_RE = re.compile(r'\bsub-?\s?batch\s+(\S+)', re.IGNORECASE)
# words that may sit between sample numbers in one list: "1, 3 and 7 to 9"
_LIST_WORDS = {'and', 'or', 'to', 'through', 'thru', 'till', 'until', '&', 'plus'}
_SHOWN_VALUES = 8


@dataclass(frozen=True)
class Grounded:
    edit: Edit
    rows: tuple[int, ...]  # Excel rows to write, in sheet order
    unchanged: tuple[int, ...] = ()  # rows already holding `new`: reported, not written
    notes: tuple[str, ...] = ()  # warnings the user sees before confirming

    @property
    def status(self) -> str:
        return 'noop' if not self.rows else 'ok'


@dataclass(frozen=True)
class Refused:
    edit: Edit
    reasons: tuple[str, ...]


class Refusal(Exception):
    """A check failed; the message is the reason the user sees."""


def ground(edit: Edit, catalog: Catalog) -> Grounded | Refused:
    """W, R, S and N on one proposal."""
    target = _column(catalog, edit.target)
    if target is None:
        return Refused(edit, (f'{edit.target} is not a column of this sheet',))
    if target.formula:  # W
        return Refused(edit, (_read_only(target, catalog),))

    try:
        rows, notes = resolve_rows(edit.rows, catalog)  # R
    except Refusal as why:
        return Refused(edit, (str(why),))

    reasons = sample_words_problems(edit, catalog, rows)  # S
    if reasons:
        return Refused(edit, tuple(reasons))

    held = {r: g.value for g in target.groups for r in g.rows}  # N
    unchanged = tuple(r for r in rows if same_value(edit.new, held.get(r)))
    if unchanged:
        which = (
            f'all {len(rows)} samples'
            if len(unchanged) == len(rows)
            else f'samples {_labels(catalog, unchanged)}'
        )
        notes = (
            *notes,
            f'{_name(target)}: already {edit.new} on {which}, nothing to change',
        )
    return Grounded(
        edit, tuple(r for r in rows if r not in unchanged), unchanged, notes
    )


# --- R rows ------------------------------------------------------------------


def resolve_rows(spec, catalog: Catalog) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """The Excel rows a row spec means, with notes (a gap in a range).
    Raises Refusal."""
    samples = catalog.samples
    if not samples:
        raise Refusal('the sheet has no sample rows')
    if spec == 'all':
        return tuple(s.row for s in samples), ()
    if isinstance(spec, Samples):
        found = [
            _one_sample(catalog, label, spec.subbatch)
            for label in dict.fromkeys(spec.samples)
        ]
        return tuple(sorted(s.row for s in found)), ()
    if isinstance(spec, Range):
        return _range_rows(catalog, spec)
    if isinstance(spec, Where):
        return _where_rows(catalog, spec)
    raise Refusal(f'unknown row form {spec!r}')


def _one_sample(catalog: Catalog, label: str, subbatch: str | None) -> SampleInfo:
    if subbatch is not None and subbatch not in {s.subbatch for s in catalog.samples}:
        raise Refusal(f'there is no subbatch {subbatch}')
    hits = [
        s
        for s in catalog.samples
        if s.label == label and (subbatch is None or s.subbatch == subbatch)
    ]
    if not hits:
        where = f' in subbatch {subbatch}' if subbatch is not None else ''
        raise Refusal(f'there is no sample {label}{where}')
    if len(hits) > 1:
        batches = ', '.join(str(s.subbatch) for s in hits)
        raise Refusal(f'sample {label} is in subbatches {batches}: say which one')
    return hits[0]


def _range_rows(catalog: Catalog, spec: Range):
    first = _one_sample(catalog, spec.first, spec.subbatch)
    last = _one_sample(catalog, spec.last, spec.subbatch)
    # the range runs in sheet order, within the subbatch when one is named
    ordered = [
        s
        for s in catalog.samples
        if spec.subbatch is None or s.subbatch == spec.subbatch
    ]
    a, b = ordered.index(first), ordered.index(last)
    if a > b:
        raise Refusal(
            f'samples {spec.first} to {spec.last}: {spec.first} comes after {spec.last} in the sheet'
        )
    chosen = ordered[a : b + 1]
    notes = ()
    missing = _missing_between(spec.first, spec.last, [s.label for s in chosen])
    if missing:
        notes = (
            f'samples {spec.first} to {spec.last}: no sample {sample_ranges(missing)}, '
            f'applied to {len(chosen)} samples',
        )
    return tuple(s.row for s in chosen), notes


def _missing_between(first: str, last: str, present: list[str]) -> list[str]:
    """Numbers from first to last that no sample carries: 20, 25 with no 22
    -> ['22']. Only when both ends are plain numbers."""
    if not (first.isdigit() and last.isdigit()):
        return []
    have = set(present)
    return [str(n) for n in range(int(first), int(last) + 1) if str(n) not in have]


def _where_rows(catalog: Catalog, spec: Where):
    column = _column(catalog, spec.where)
    if column is None:
        raise Refusal(f'{spec.where} is not a column of this sheet')
    rows = tuple(
        r
        for g in column.groups
        for r in g.rows
        if (g.value is None and spec.equals is None)
        or (spec.equals is not None and same_value(spec.equals, g.value))
    )
    if not rows:
        wanted = 'empty' if spec.equals is None else repr(spec.equals)
        shown = [repr(_text(g.value)) for g in column.groups[:_SHOWN_VALUES]]
        more = ', ...' if len(column.groups) > _SHOWN_VALUES else ''
        raise Refusal(
            f'no sample has {_name(column)} = {wanted}; it holds {", ".join(shown)}{more}'
        )
    return tuple(sorted(rows)), ()


# --- S sample words -----------------------------------------------------------


def sample_words_problems(
    edit: Edit, catalog: Catalog, rows: tuple[int, ...]
) -> list[str]:
    """The samples named in `said` must be exactly the ones in `rows`.
    'all' is allowed only when no sample was named."""
    said = said_samples(edit.said, catalog)
    spec = edit.rows
    if spec == 'all' or isinstance(spec, Where):
        if said:
            labels = sample_ranges([s.label for s in said])
            how = 'all samples' if spec == 'all' else 'samples chosen by a value'
            return [f'you named samples {labels}, but the edit would apply to {how}']
        return []
    if (
        spec.subbatch is not None
    ):  # the numbers only make sense once the subbatch is known
        m = _SUBBATCH_RE.search(edit.said)
        if not m or m.group(1).strip('.,;') != spec.subbatch:
            return [f'you did not name subbatch {spec.subbatch}']
    chosen = {s.row: s for s in catalog.samples if s.row in rows}
    named = {s.row for s in said}
    problems = []
    if extra := sorted(named - set(chosen)):
        problems.append(
            f'you named samples {_labels(catalog, extra)}, which the edit does not include'
        )
    if missing := sorted(set(chosen) - named):
        if isinstance(spec, Range) and not said:
            problems.append(
                f'samples {spec.first} to {spec.last}: you did not name them'
            )
        elif not isinstance(spec, Range):
            problems.append(
                f'the edit includes samples {_labels(catalog, missing)}, which you did not name'
            )
    return problems


def said_samples(said: str, catalog: Catalog) -> list[SampleInfo]:
    """Samples the user named: numbers listed after 'sample(s)', ranges
    among them expanded in sheet order, labels and Nomad IDs written out.
    A subbatch named in the words ('of subbatch 1') scopes the numbers."""
    m = _SUBBATCH_RE.search(said)
    subbatch = m.group(1).strip('.,;') if m else None
    ordered = [s for s in catalog.samples if subbatch is None or s.subbatch == subbatch]

    def by_number(number: Decimal) -> SampleInfo | None:
        if number != number.to_integral_value():
            return None
        hits = [s for s in ordered if s.label == str(int(number))]
        return (
            hits[0] if len(hits) == 1 else None
        )  # None also when in several subbatches

    found: dict[int, SampleInfo] = {}
    for word in _SAMPLE_WORD_RE.finditer(said):
        clause = _sample_clause(said[word.end() :])
        spans = {(n.start, n.end): n for n in numbers(clause) if n.unit is None}
        for r in ranges(clause):
            a, b = by_number(r.first.value), by_number(r.last.value)
            if a and b and ordered.index(a) <= ordered.index(b):
                for s in ordered[ordered.index(a) : ordered.index(b) + 1]:
                    found[s.row] = s
            spans.pop((r.first.start, r.first.end), None)
            spans.pop((r.last.start, r.last.end), None)
        for n in spans.values():
            s = by_number(n.value)
            if s:
                found[s.row] = s
    for s in catalog.samples:  # 's_7', '07' or a Nomad ID written as it is
        for literal in (s.label, s.nomad_id):
            if (
                literal
                and not literal.isdigit()
                and re.search(rf'(?<!\w){re.escape(literal)}(?!\w)', said)
            ):
                found[s.row] = s
    return [found[r] for r in sorted(found)]


def _sample_clause(text: str) -> str:
    """The list of numbers right after 'samples': stops at the first word
    that is not a number, a range or a list word."""
    pos = 0
    for m in re.finditer(r'\S+', text):
        word = m.group().strip('.,;:')
        is_number = bool(numbers(word)) or bool(
            re.fullmatch(r'[\d.,]+[-–—]?[\d.,]*', word)
        )
        if not (
            is_number
            or word.lower() in _LIST_WORDS
            or re.fullmatch(r'[-–—,&]+', m.group())
        ):
            break
        pos = m.end()
    return text[:pos]


# --- values ------------------------------------------------------------------


def same_value(new: str, stored) -> bool:
    """Whether the text the user gave equals the stored cell: numbers
    numerically ('3000' is 3000.0), text exactly, empty only to empty."""
    if stored is None or (isinstance(stored, str) and not stored.strip()):
        return new.strip() == ''
    if isinstance(stored, bool):
        return new.strip().upper() == ('TRUE' if stored else 'FALSE')
    if isinstance(stored, int | float):
        n = value(new)
        return n is not None and n.value == Decimal(str(stored))
    if isinstance(stored, str):
        return new.strip() == stored.strip()
    return False


# --- helpers -----------------------------------------------------------------


def _column(catalog: Catalog, column_id: str) -> ColumnInfo | None:
    return next((c for c in catalog.columns if c.id == column_id), None)


def _name(column: ColumnInfo) -> str:
    return f'{column.column.header} ({column.column.section.label})'


def _read_only(target: ColumnInfo, catalog: Catalog) -> str:
    inputs = [_column(catalog, i) for i in target.inputs or ()]
    names = ', '.join(c.column.header for c in inputs if c)
    hint = f' from {names}; edit {names} instead' if names else ''
    return f'{_name(target)} is calculated by a formula{hint}'


def _labels(catalog: Catalog, rows) -> str:
    by_row = {s.row: s.label or f'row {s.row}' for s in catalog.samples}
    return sample_ranges([by_row[r] for r in rows])


def _text(v) -> str:
    return '' if v is None else str(v)
