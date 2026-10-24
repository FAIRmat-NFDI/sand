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

RowsAndNotes = tuple[tuple[int, ...], tuple[str, ...]]


@dataclass(frozen=True)
class Grounded:
    """A proposal that passed: which rows to write, which to skip, what to
    warn about."""

    edit: Edit
    rows: tuple[int, ...]  # Excel rows to write, in sheet order
    unchanged: tuple[int, ...] = ()  # rows already holding `new`: reported, not written
    notes: tuple[str, ...] = ()  # warnings the user sees before confirming

    @property
    def status(self) -> str:
        return 'noop' if not self.rows else 'ok'


@dataclass(frozen=True)
class Refused:
    """A proposal that failed a check; `reasons` is what the user reads."""

    edit: Edit
    reasons: tuple[str, ...]


class Refusal(Exception):
    """Raised inside a check when it fails; ground() turns it into Refused."""


def ground(edit: Edit, catalog: Catalog) -> Grounded | Refused:
    """Check one proposal against the sheet, in this order: the target can
    be written (W), the rows exist (R), the samples the user named are the
    rows used (S), rows already holding the value are set aside (N)."""
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
            else f'samples {catalog.names(unchanged)}'
        )
        notes = (
            *notes,
            f'{_name(target)}: already {edit.new} on {which}, nothing to change',
        )
    return Grounded(
        edit, tuple(r for r in rows if r not in unchanged), unchanged, notes
    )


# --- R rows ------------------------------------------------------------------


def resolve_rows(spec, catalog: Catalog) -> RowsAndNotes:
    """Turn a row spec ('all', samples, a range, a condition) into Excel
    row numbers, plus notes to show (a gap in a range). Raises Refusal
    when a sample, subbatch or matching row does not exist."""
    samples = catalog.samples
    if not samples:
        raise Refusal('the sheet has no sample rows')
    if spec == 'all':
        return tuple(s.row for s in samples), ()
    if isinstance(spec, Samples):
        found = [
            sample_by_label(catalog, label, spec.subbatch)
            for label in dict.fromkeys(spec.samples)
        ]
        return tuple(sorted(s.row for s in found)), ()
    if isinstance(spec, Range):
        return _range_rows(catalog, spec)
    if isinstance(spec, Where):
        return _where_rows(catalog, spec)
    raise Refusal(f'unknown row form {spec!r}')


def sample_by_label(catalog: Catalog, label: str, subbatch: str | None) -> SampleInfo:
    """The sample called `label` - the name in the Sample column ('7',
    's_7'), not the Excel row. Refuses when there is none, or when the name
    is in several subbatches and no subbatch was given."""
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


def _range_rows(catalog: Catalog, spec: Range) -> RowsAndNotes:
    """Rows from `first` to `last` as they stand in the sheet. A missing
    number in between (no sample 22) is a note, not an error."""
    first = sample_by_label(catalog, spec.first, spec.subbatch)
    last = sample_by_label(catalog, spec.last, spec.subbatch)
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


def _where_rows(catalog: Catalog, spec: Where) -> RowsAndNotes:
    """Rows where a column holds exactly `equals` (None: is empty). When no
    row matches, the refusal lists what the column does hold."""
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
    """Compare the samples the user named with the rows the edit uses.
    Returns the differences as messages; empty means they agree. 'all'
    and a condition are fine only when no sample or subbatch was named."""
    said = said_samples(edit.said, catalog)
    spec = edit.rows
    problems = []
    if said.unknown:
        problems.append(f'there is no sample {", ".join(said.unknown)}')
    named = {s.row for s in said.samples}
    if spec == 'all' or isinstance(spec, Where):
        how = 'all samples' if spec == 'all' else 'samples chosen by a value'
        if named:
            problems.append(
                f'you named samples {catalog.names(sorted(named))}, '
                f'but the edit would apply to {how}'
            )
        if said.subbatch is not None and len({s.subbatch for s in catalog.samples}) > 1:
            problems.append(
                f'you named subbatch {said.subbatch}, '
                f'but the edit would apply to {how} of every subbatch'
            )
        return problems
    if spec.subbatch is not None and said.subbatch != spec.subbatch:
        # the numbers only make sense once the subbatch is known
        return [f'you did not name subbatch {spec.subbatch}']
    if extra := sorted(named - set(rows)):
        problems.append(
            f'you named samples {catalog.names(extra)}, which the edit does not include'
        )
    if missing := sorted(set(rows) - named):
        if isinstance(spec, Range) and not named:
            problems.append(
                f'samples {spec.first} to {spec.last}: you did not name them'
            )
        else:
            problems.append(
                f'the edit includes samples {catalog.names(missing)}, which you did not name'
            )
    return problems


@dataclass(frozen=True)
class Said:
    """What the user's words name."""

    samples: tuple[SampleInfo, ...]  # found in the sheet, in sheet order
    unknown: tuple[str, ...]  # numbers after 'sample(s)' that no sample carries
    subbatch: str | None  # 'of subbatch 1'


def said_samples(said: str, catalog: Catalog) -> Said:
    """Find the samples the user named in their words: numbers right after
    'sample(s)' ("samples 1, 3 and 7 to 9", ranges expanded in sheet
    order), and labels or Nomad IDs written out ('s_7'). A subbatch in the
    words ('of subbatch 1') limits where the numbers are looked up; without
    one, a number found in several subbatches names all of them."""
    subbatch = _said_subbatch(said)
    ordered = [s for s in catalog.samples if subbatch is None or s.subbatch == subbatch]

    def by_number(number: Decimal) -> list[SampleInfo]:
        # '7' and '07' both mean sample 7
        return [
            s
            for s in ordered
            if s.label and s.label.isdigit() and Decimal(s.label) == number
        ]

    found: dict[int, SampleInfo] = {}
    unknown: list[str] = []
    for word in _SAMPLE_WORD_RE.finditer(said):
        clause = _sample_clause(said[word.end() :])
        spans = {(n.start, n.end): n for n in numbers(clause) if n.unit is None}
        value_ends = set()  # numbers that are a value, not a sample
        for r in ranges(clause):
            ends = ((r.first.start, r.first.end), (r.last.start, r.last.end))
            a, b = by_number(r.first.value), by_number(r.last.value)
            if (
                len(a) == 1
                and len(b) == 1
                and ordered.index(a[0]) <= ordered.index(b[0])
            ):
                for s in ordered[ordered.index(a[0]) : ordered.index(b[0]) + 1]:
                    found[s.row] = s
                for span in ends:
                    spans.pop(span, None)
            else:  # "samples 1 and 2 to 3000": 2 is a sample, 3000 the value
                value_ends.update(ends)
        for span, n in spans.items():
            hits = by_number(n.value)
            for s in hits:
                found[s.row] = s
            if (
                not hits
                and span not in value_ends
                and n.value == n.value.to_integral_value()
            ):
                unknown.append(n.text)
    for s in catalog.samples:  # 's_7' or a Nomad ID written as it is
        for literal in (s.label, s.nomad_id):
            if (
                literal
                and not literal.isdigit()
                and re.search(rf'(?<!\w){re.escape(literal)}(?!\w)', said)
            ):
                found[s.row] = s
    return Said(
        tuple(found[r] for r in sorted(found)), tuple(dict.fromkeys(unknown)), subbatch
    )


def _said_subbatch(text: str) -> str | None:
    m = _SUBBATCH_RE.search(text)
    return m.group(1).strip('.,;') if m else None


def _sample_clause(text: str) -> str:
    """Cut the number list right after 'samples': "1, 3 and 7 to 9 to
    3000 rpm" -> "1, 3 and 7 to 9 to". Stops at the first word that is not
    a number, a dash or a list word."""
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
    """Is the text the user gave the same as what the cell holds? Numbers
    compare as numbers ('3000' is 3000.0), text exactly, TRUE/FALSE as
    words, empty only to empty."""
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
    """How a column is named in messages: 'Spin speed [rpm] (2: Spin Coating)'."""
    return f'{column.column.header} ({column.column.section.label})'


def _read_only(target: ColumnInfo, catalog: Catalog) -> str:
    """The message for a formula column, naming the columns to edit instead."""
    inputs = [_column(catalog, i) for i in target.inputs or ()]
    names = ', '.join(c.column.header for c in inputs if c)
    hint = f' from {names}; edit {names} instead' if names else ''
    return f'{_name(target)} is calculated by a formula{hint}'


def _text(v) -> str:
    return '' if v is None else str(v)
