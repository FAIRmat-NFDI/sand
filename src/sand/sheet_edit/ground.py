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

from sand.sheet_edit.catalog import (
    Catalog,
    ColumnInfo,
    SampleInfo,
    numbered,
    sample_ranges,
)
from sand.sheet_edit.edits import Edit, Range, Samples, Where
from sand.sheet_edit.numbers import RANGE_WORDS, numbers, value

_SAMPLE_WORD_RE = re.compile(r'\bsamples?(?=\d|\b)', re.IGNORECASE)  # also 'sample1'
_SUBBATCH_RE = re.compile(r'\bsub-?\s?batch(?:es)?:?\s+', re.IGNORECASE)
_EXCEPT_RE = re.compile(
    r'\b(?:except|excluding|but not|without|apart from|other than)\b\s*(?:samples?\s+)?',
    re.IGNORECASE,
)
# "all samples", "every sample", "all of the samples", "the whole subbatch"
_ALL_RE = re.compile(
    r'\b(?:all|every|each)\b(?:\s+\w+){0,2}?\s+samples?\b|\b(?:whole|entire)\s+sub-?\s?batch',
    re.IGNORECASE,
)
# words that may sit between sample numbers in one list: "1, 3 and 7 to 9"
_LIST_WORDS = {'and', 'or', 'to', 'through', 'thru', 'till', 'until', '&', 'plus'}
_LIST_WORDS |= {'no', 'number', 'nr', '#'}  # "sample no. 3"
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
    excluded = {s.row for s in said.excluded}
    if spec == 'all' or isinstance(spec, Where):
        how = 'all samples' if spec == 'all' else 'samples chosen by a value'
        return problems + _nothing_named_problems(said, catalog, how)
    if spec.subbatch is not None:
        if spec.subbatch not in said.subbatches:
            # the numbers only make sense once the subbatch is known
            return [f'you did not name subbatch {spec.subbatch}']
        # "samples 1 to 3 of subbatches 0 and 1" is one edit per subbatch
        in_subbatch = {s.row for s in catalog.samples if s.subbatch == spec.subbatch}
        named, excluded = named & in_subbatch, excluded & in_subbatch
    if not named and said.all:  # "all samples of subbatch 1": the whole scope
        named = set(_scope(catalog, spec.subbatch, said.subbatches)) - excluded
    if hit := sorted(excluded & set(rows)):
        problems.append(
            f'you excluded samples {catalog.names(hit)}, but the edit includes them'
        )
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


def _nothing_named_problems(said: 'Said', catalog: Catalog, how: str) -> list[str]:
    """'all' and a condition cover rows the user did not pick one by one,
    so naming samples, exclusions or a subbatch contradicts them."""
    problems = []
    if said.samples:
        rows = [s.row for s in said.samples]
        problems.append(
            f'you named samples {catalog.names(rows)}, but the edit would apply to {how}'
        )
    if said.excluded:
        rows = [s.row for s in said.excluded]
        problems.append(
            f'you excluded samples {catalog.names(rows)}, but the edit would apply to {how}'
        )
    if said.subbatches and len({s.subbatch for s in catalog.samples}) > 1:
        problems.append(
            f'you named subbatch {", ".join(said.subbatches)}, '
            f'but the edit would apply to {how} of every subbatch'
        )
    return problems


def _scope(catalog: Catalog, subbatch: str | None, said: tuple[str, ...]) -> list[int]:
    """The rows 'all samples' means: of the edit's subbatch, else of the
    subbatches the words name, else the whole sheet."""
    wanted = {subbatch} if subbatch is not None else set(said)
    return [s.row for s in catalog.samples if not wanted or s.subbatch in wanted]


@dataclass(frozen=True)
class Said:
    """What the user's words name."""

    samples: tuple[SampleInfo, ...]  # named, in sheet order, minus the excluded
    excluded: tuple[SampleInfo, ...]  # "except 3"
    unknown: tuple[str, ...]  # numbers after 'sample(s)' that no sample carries
    subbatches: tuple[str, ...]  # "of subbatch 1", "subbatches 0 and 1"
    all: bool  # "all samples", "the whole subbatch"


def said_samples(said: str, catalog: Catalog) -> Said:
    """Find the samples the user named in their words: what is listed right
    after 'sample(s)' ("samples 1, 3 and 7 to 9", "samples s_1 to s_3";
    ranges expanded in sheet order), and labels or Nomad IDs written out
    anywhere ('s_7', 'KIT_Synth_1'). A number names the sample with that
    number: '7' is sample '7', '07' or 's_7'. A subbatch in the words ('of
    subbatch 1') limits where names are looked up; without one, a name
    found in several subbatches names all of them."""
    subbatches = _said_subbatches(said)
    ordered = [s for s in catalog.samples if not subbatches or s.subbatch in subbatches]
    found, unknown = _after(_SAMPLE_WORD_RE, said, ordered)
    excluded, unknown_excluded = _after(_EXCEPT_RE, said, ordered)
    pattern = _literal_re(ordered)  # 's_7', 'ref' or a Nomad ID written out
    for m in pattern.finditer(said) if pattern else ():
        for s in _by_literal(ordered, m.group()):
            found[s.row] = s
    return Said(
        samples=tuple(found[r] for r in sorted(found) if r not in excluded),
        excluded=tuple(excluded[r] for r in sorted(excluded)),
        unknown=tuple(dict.fromkeys(unknown + unknown_excluded)),
        subbatches=subbatches,
        all=bool(_ALL_RE.search(said)),
    )


def _after(
    pattern: re.Pattern, said: str, ordered: list[SampleInfo]
) -> tuple[dict[int, SampleInfo], list[str]]:
    """Samples listed after each match of `pattern` ('samples', 'except'),
    by row, and the listed names no sample carries."""
    found: dict[int, SampleInfo] = {}
    unknown: list[str] = []
    for word in pattern.finditer(said):
        clause = _sample_clause(said[word.end() :], ordered)
        for item in _listed(clause, ordered):
            for s in item.hits:
                found[s.row] = s
            if not item.hits:
                unknown.append(item.text)
    return found, unknown


@dataclass(frozen=True)
class _Item:
    """One thing listed after 'samples' - a number or a label - and the
    samples it names (none when the sheet has no such sample)."""

    start: int
    end: int
    text: str
    hits: tuple[SampleInfo, ...]


def _listed(clause: str, ordered: list[SampleInfo]) -> list[_Item]:
    """The items after 'samples' with ranges expanded: "1, 3 and 7 to 9"
    -> 1, 3, 7, 8, 9. A number after a range word that is no sample is a
    value ("samples 1 and 2 to 3000", "samples 1 to 3 to 3000"), not an
    unknown sample."""
    items = _items(clause, ordered)
    linked = [False] + [
        clause[a.end : b.start].strip().lower() in RANGE_WORDS
        for a, b in zip(items, items[1:])
    ]
    out = []
    i = 0
    while i < len(items):
        item = items[i]
        after = items[i + 1] if i + 1 < len(items) else None
        if after and linked[i + 1] and (span := _between(item, after, ordered)):
            out.append(
                _Item(item.start, after.end, clause[item.start : after.end], span)
            )
            i += 2
            continue
        if not (linked[i] and not item.hits):
            out.append(item)
        i += 1
    return out


def _between(a: _Item, b: _Item, ordered: list[SampleInfo]) -> tuple[SampleInfo, ...]:
    """The samples from a to b in sheet order, within each subbatch where
    both are exactly one sample ("samples 1 to 3 of subbatches 0 and 1").
    Empty when the range cannot be followed (backwards, or an end is no
    sample)."""
    rows = []
    for subbatch in dict.fromkeys(s.subbatch for s in a.hits):
        first = [s for s in a.hits if s.subbatch == subbatch]
        last = [s for s in b.hits if s.subbatch == subbatch]
        if len(first) != 1 or len(last) != 1:
            continue
        lo, hi = ordered.index(first[0]), ordered.index(last[0])
        rows += [s for s in ordered[lo : hi + 1] if s.subbatch == subbatch]
    return tuple(rows)


def _items(clause: str, ordered: list[SampleInfo]) -> list[_Item]:
    """Numbers and labels in the clause, in order, each with the samples it
    names."""
    labels = {s.label for s in ordered if s.label}
    items = []
    for m in re.finditer(r'\S+', clause):
        token = m.group().strip('.,;:')
        hits = _by_literal(ordered, token)
        if hits or _prefixed(token, labels):
            start = m.start() + m.group().index(token)
            items.append(_Item(start, start + len(token), token, hits))
    for n in numbers(clause):
        if n.unit is not None or n.value != n.value.to_integral_value():
            continue
        if any(i.start <= n.start and n.end <= i.end for i in items):
            continue  # the 1 in 'A-1'
        items.append(_Item(n.start, n.end, n.text, _by_number(ordered, n.value)))
    return sorted(items, key=lambda i: i.start)


def _by_number(ordered: list[SampleInfo], number: Decimal) -> tuple[SampleInfo, ...]:
    """The samples a bare number names: label '7', '07' or 's_7' for 7."""
    hits = []
    for s in ordered:
        if not s.label:
            continue
        if s.label.isdigit():
            same = Decimal(s.label) == number
        else:
            parts = numbered(s.label)
            same = parts is not None and parts[1] == number
        if same:
            hits.append(s)
    return tuple(hits)


def _by_literal(ordered: list[SampleInfo], text: str) -> tuple[SampleInfo, ...]:
    """Samples whose label or Nomad ID is `text`; the exact spelling wins,
    else any spelling ('a' names 'A' unless there is also an 'a')."""
    exact = tuple(s for s in ordered if text in (s.label, s.nomad_id))
    if exact:
        return exact
    low = text.lower()
    return tuple(
        s
        for s in ordered
        if low in {(s.label or '').lower(), (s.nomad_id or '').lower()}
    )


def _literal_re(ordered: list[SampleInfo]) -> re.Pattern | None:
    """Matches any non-numeric label or Nomad ID as a whole word, longest
    first so 'Ctrl-2' is not read as 'Ctrl'."""
    words = {w for s in ordered for w in (s.label, s.nomad_id) if w and not w.isdigit()}
    if not words:
        return None
    longest_first = sorted(words, key=len, reverse=True)
    pattern = '|'.join(map(re.escape, longest_first))
    return re.compile(rf'(?<!\w)(?:{pattern})(?!\w)', re.IGNORECASE)


def _prefixed(token: str, labels: set[str]) -> bool:
    """Whether a token is shaped like the sheet's labels ('s_6' among
    's_1'..'s_13'), so a label that does not exist is still noticed."""
    parts = numbered(token)
    if parts is None or not parts[0]:
        return False
    return any((p := numbered(label)) and p[0] == parts[0] for label in labels)


def _said_subbatches(text: str) -> tuple[str, ...]:
    """'of subbatch 1' -> ('1',); 'subbatches 0 and 1' -> ('0', '1')."""
    out = []
    for m in _SUBBATCH_RE.finditer(text):
        for i, w in enumerate(re.finditer(r'\S+', text[m.end() :])):
            word = re.sub(r"'s$", '', w.group().strip('.,;:'))
            joiner = word.lower() in _LIST_WORDS or re.fullmatch(r'[-–—,&]+', word)
            if i and not (joiner or numbers(word)):
                break
            if word and not joiner:
                out.append(word)
    return tuple(dict.fromkeys(out))


def _sample_clause(text: str, ordered: list[SampleInfo]) -> str:
    """Cut the list right after 'samples': "1, 3 and 7 to 9 at 3000 rpm"
    -> "1, 3 and 7 to 9". Stops at the first word that is not a number,
    a label, a dash or a list word, and before a number with a unit."""
    labels = {s.label for s in ordered if s.label}
    pos = 0
    for m in re.finditer(r'\S+', text):
        word = m.group().strip('.,;:')
        is_number = bool(numbers(word)) or bool(
            re.fullmatch(r'[\d.,]+[-–—]?[\d.,]*', word)
        )
        if is_number and _has_unit(text[m.start() :]):
            break  # "samples 1 to 3 4000 rpm": the value ends the list
        if not (
            is_number
            or _by_literal(ordered, word)
            or _prefixed(word, labels)
            or word.lower() in _LIST_WORDS
            or re.fullmatch(r'[-–—,&]+', m.group())
        ):
            break
        pos = m.end()
        if m.group().endswith(':'):
            break  # "samples 1 to 3: 4000"
    return text[:pos]


def _has_unit(text: str) -> bool:
    """Whether the number the text starts with carries a unit ('20 min')."""
    first = next(iter(numbers(text)), None)
    return first is not None and first.start == 0 and first.unit is not None


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
