"""Code checks every proposal against the real sheet before anything is
written.

Structure: W the target is writable, R the rows exist and are found by
exact match, S the samples the user named are the ones used. Value: A
the anchor ("the volume of the DMSO") holds and points at the target, O
`old` is what the cell holds, V `new` is in the user's words, T it fits
the column's type, U its unit is the column's. N rows that already hold
the value are reported, not written.

A check refuses only on evidence against the proposal. Where code finds
nothing to compare ("the usual speed" names no number) the edit passes
with a doubt and low confidence: the user confirms it once.
"""

import datetime
import re
from dataclasses import dataclass
from decimal import Decimal

from sand.sheet_edit.catalog import (
    Catalog,
    ColumnInfo,
    SampleInfo,
    is_day,
    numbered,
    sample_ranges,
)
from sand.sheet_edit.edits import Confidence, Edit, Range, Samples, Where
from sand.sheet_edit.numbers import (
    RANGE_WORDS,
    Number,
    UnitMismatch,
    check_unit,
    numbers,
    value,
)
from sand.sheet_edit.render import EMPTY

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
_STEP_WORDS = {'step', 'section'}  # words a step number follows

RowsAndNotes = tuple[tuple[int, ...], tuple[str, ...]]


@dataclass(frozen=True)
class Grounded:
    """A proposal that passed: which rows to write, what to write, what to
    warn about, and what the user should confirm."""

    edit: Edit
    rows: tuple[int, ...]  # Excel rows to write, in sheet order
    value: object  # what is written: 4000, 'DMSO', True; None clears the cell
    unchanged: tuple[int, ...] = ()  # rows already holding `new`: reported, not written
    notes: tuple[str, ...] = ()  # warnings the user sees before confirming
    doubts: tuple[str, ...] = ()  # what code could not check in the words

    @property
    def status(self) -> str:
        return 'noop' if not self.rows else 'ok'

    @property
    def confidence(self) -> Confidence:
        """low: confirm one by one; medium: glance; high: can be skipped."""
        if self.doubts:
            return 'low'
        return 'medium' if self.notes else 'high'


@dataclass(frozen=True)
class Refused:
    """A proposal that failed a check; `reasons` is what the user reads."""

    edit: Edit
    reasons: tuple[str, ...]


class Refusal(Exception):
    """Raised inside a check when it fails; ground() turns it into Refused."""


def ground(edit: Edit, catalog: Catalog) -> Grounded | Refused:
    """Check one proposal against the sheet: the target can be written
    (W), the rows exist (R), the samples the user named are the rows used
    (S), the anchor holds (A), `old` is what the cell holds (O), `new` is
    in the words (V), fits the column (T) and its unit (U); rows already
    holding the value are set aside (N)."""
    target = catalog.column(edit.target)
    if target is None:
        return Refused(edit, (f'{edit.target} is not a column of this sheet',))
    if target.formula:  # W
        return Refused(edit, (_read_only(target, catalog),))

    try:
        rows, notes = resolve_rows(edit.rows, catalog)  # R
    except Refusal as why:
        return Refused(edit, (str(why),))

    problems, doubts = sample_words_problems(edit, catalog, rows)  # S
    anchor = None
    if edit.anchor is not None:  # A
        anchor = catalog.column(edit.anchor.column)
        found = anchor_problems(edit, target, anchor, catalog, rows)
        problems, doubts = problems + found[0], doubts + found[1]
    problems += old_problems(edit, target, catalog, rows)  # O
    found = value_problems(edit, target, anchor, catalog)  # V
    problems, doubts = problems + found[0], doubts + found[2]
    notes = (*notes, *found[1])
    new, problems_t = typed_value(edit.new, target)  # T, U
    problems += problems_t
    if problems:
        return Refused(edit, tuple(problems))

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
            f'{column_name(target)}: already {edit.new or "empty"} on {which}, nothing to change',
        )
    return Grounded(
        edit,
        tuple(r for r in rows if r not in unchanged),
        new,
        unchanged,
        notes,
        tuple(doubts),
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
    column = catalog.column(spec.where)
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
            f'no sample has {column_name(column)} = {wanted}; it holds {", ".join(shown)}{more}'
        )
    return tuple(sorted(rows)), ()


# --- S sample words -----------------------------------------------------------


def sample_words_problems(
    edit: Edit, catalog: Catalog, rows: tuple[int, ...]
) -> tuple[list[str], list[str]]:
    """Compare the samples the user named with the rows the edit uses.
    Returns (problems, doubts): problems where the words contradict the
    rows, doubts where the words name nothing the code can read ("the
    first three samples"). 'all' and a condition are fine only when no
    sample or subbatch was named."""
    said = said_samples(edit.said, catalog)
    spec = edit.rows
    problems, doubts = [], []
    subbatches = {s.subbatch for s in catalog.samples}
    if unknown := [b for b in said.subbatches if b not in subbatches]:
        return [f'there is no subbatch {", ".join(unknown)}'], []
    if said.unknown:
        problems.append(f'there is no sample {", ".join(said.unknown)}')
    named = {s.row for s in said.samples}
    excluded = {s.row for s in said.excluded}
    if spec == 'all' or isinstance(spec, Where):
        how = 'all samples' if spec == 'all' else 'samples chosen by a value'
        return problems + _nothing_named_problems(said, catalog, how), []
    if spec.subbatch is not None:
        problems += _subbatch_problems(spec.subbatch, said.subbatches, doubts)
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
        if named:  # the user listed samples; the edit adds others
            problems.append(
                f'the edit includes samples {catalog.names(missing)}, which you did not name'
            )
        elif isinstance(spec, Range):
            doubts.append(
                f'samples {spec.first} to {spec.last}: not named in your words'
            )
        else:
            doubts.append(f'samples {catalog.names(missing)}: not named in your words')
    return problems, doubts


def _subbatch_problems(
    wanted: str, said: tuple[str, ...], doubts: list[str]
) -> list[str]:
    """The edit's subbatch against the words: another one named is a
    contradiction, none named a doubt."""
    if wanted in said:
        return []
    if said:
        return [f'you named subbatch {", ".join(said)}, not {wanted}']
    doubts.append(f'subbatch {wanted}: not named in your words')
    return []


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
    if not text:
        return ()
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
    """Matches a non-numeric label or Nomad ID written as it is, anywhere
    in the words; longest first so 'Ctrl-2' is not read as 'Ctrl'. Exact
    spelling only, and no one-letter labels: 'a sample' is not sample A."""
    words = {
        w
        for s in ordered
        for w in (s.label, s.nomad_id)
        if w and len(w) > 1 and not w.isdigit()
    }
    if not words:
        return None
    longest_first = sorted(words, key=len, reverse=True)
    pattern = '|'.join(map(re.escape, longest_first))
    return re.compile(rf'(?<!\w)(?:{pattern})(?!\w)')


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
            or not word  # "samples: 1", "1 , 3"
        ):
            break
        pos = m.end()
        if word and m.group().endswith(':'):
            break  # "samples 1 to 3: 4000"
    return text[:pos]


def _has_unit(text: str) -> bool:
    """Whether the number the text starts with carries a unit ('20 min')."""
    first = next(iter(numbers(text)), None)
    return first is not None and first.start == 0 and first.unit is not None


# --- A anchor ----------------------------------------------------------------


def anchor_problems(
    edit: Edit,
    target: ColumnInfo,
    anchor: ColumnInfo | None,
    catalog: Catalog,
    rows: tuple[int, ...],
) -> tuple[list[str], list[str]]:
    """ "The volume of the DMSO": the anchor column must hold DMSO in the
    targeted rows and sit in the target's step; a numbered anchor
    (Solvent 2 name) reaches only its own number (Solvent 2 volume).
    Returns (problems, doubts); the doubt: the anchor value is not in the
    words."""
    assert edit.anchor is not None
    if anchor is None:
        return [f'{edit.anchor.column} is not a column of this sheet'], []
    problems = []
    if anchor.column.section != target.column.section:
        problems.append(
            f'{column_name(anchor)} is not in the step of {column_name(target)}'
        )
    elif (
        anchor.family is not None
        and target.family is not None
        and anchor.family.index != target.family.index
    ):
        problems.append(
            f'{anchor.column.header} is number {anchor.family.index}, '
            f'{target.column.header} is number {target.family.index}'
        )
    wrong = _rows_not_holding(anchor, edit.anchor.equals, rows)
    if wrong:
        problems.append(
            f'{column_name(anchor)} holds {_held(anchor, catalog, wrong)}, '
            f'not {edit.anchor.equals!r}'
        )
    doubts = []
    if not in_words(edit.anchor.equals, edit.said):
        doubts.append(f'{edit.anchor.equals!r}: not found in your words')
    return problems, doubts


# --- O old value ---------------------------------------------------------------


def old_problems(
    edit: Edit, target: ColumnInfo, catalog: Catalog, rows: tuple[int, ...]
) -> list[str]:
    """`old` must be what the target holds in every targeted row that
    does not already hold `new`; otherwise the LLM read another line of
    the table."""
    wrong = _rows_not_holding(target, edit.old or '', rows)
    wrong = [r for r in wrong if r in _rows_not_holding(target, edit.new, rows)]
    if not wrong:
        return []
    claimed = 'empty' if not edit.old else repr(edit.old)
    return [
        f'{column_name(target)} holds {_held(target, catalog, wrong)}, not {claimed}'
    ]


def _rows_not_holding(column: ColumnInfo, text: str, rows) -> list[int]:
    held = {r: g.value for g in column.groups for r in g.rows}
    return [r for r in rows if not same_value(text, held.get(r))]


def _held(column: ColumnInfo, catalog: Catalog, rows: list[int]) -> str:
    """ "'3000' on samples 1–3 and '3500' on samples 4–5"."""
    by_value: dict[str, list[int]] = {}
    for g in column.groups:
        for r in g.rows:
            if r in rows:
                by_value.setdefault(_text(g.value), []).append(r)
    return ' and '.join(
        f'{v!r} on samples {catalog.names(sorted(rs))}' for v, rs in by_value.items()
    )


# --- V value in the words --------------------------------------------------------

_CLEAR_WORDS = re.compile(r'\b(?:clear|empty|blank|delete|remove|erase|none)\b', re.I)
_TRUE_WORDS = re.compile(r'\b(?:true|yes|on|enabled?|checked)\b', re.I)
_FALSE_WORDS = re.compile(r'\b(?:false|no|off|disabled?|unchecked)\b', re.I)


def value_problems(
    edit: Edit, target: ColumnInfo, anchor: ColumnInfo | None, catalog: Catalog
) -> tuple[list[str], list[str], list[str]]:
    """`new` must come from the user's words. A number is compared with the
    numbers said that are values (not sample numbers, not the 2 of
    'solvent 2'); text is looked for as written. Returns (problems,
    notes, doubts): a problem when the words hold other numbers but not
    this one; a note when it was read from words ('four thousand'); a
    doubt when the words hold nothing to compare."""
    new, said = edit.new.strip(), edit.said
    where = f'{column_name(target)}: {new!r}'
    doubt = [f'{where} not found in your words']
    if new == '':
        return [], [], [] if _CLEAR_WORDS.search(said) else doubt
    if target.type == 'bool':
        words = _TRUE_WORDS if new.upper() in ('TRUE', 'YES') else _FALSE_WORDS
        return [], [], [] if words.search(said) else doubt
    number = None if target.type == 'text' else value(new)  # text: as written
    if number is None:
        return [], [], [] if in_words(new, said) else doubt
    candidates = value_numbers(said, target, anchor, catalog)
    if target.type == 'yyyymmdd':  # only a day written as 20250101 compares
        candidates = [c for c in candidates if is_day(str(c.value))]
    matches = [c for c in candidates if c.value == number.value]
    if matches:
        spoken = [m.text for m in matches if not any(ch.isdigit() for ch in m.text)]
        notes = (
            [f'{where}: read from {spoken[0]!r}'] if len(spoken) == len(matches) else []
        )
        return [], notes, []
    if candidates:
        said_numbers = ', '.join(c.text for c in candidates)
        return [f'{where} is not in your words; you said {said_numbers}'], [], []
    # nothing to compare: no number said, or a date said in words ('2nd of
    # February 2025'), which code does not read yet
    return [], [], doubt


def value_numbers(
    said: str, target: ColumnInfo, anchor: ColumnInfo | None, catalog: Catalog
) -> list[Number]:
    """The numbers in the words that can be a value: not after 'samples'
    or 'subbatch', and not a number of the column's name next to one of
    its words (the 2 in 'solvent 2 volume', 'spin coating 2 speed' or
    'step 2'). Positions are in `said`."""
    blanked = said
    ordered = list(catalog.samples)
    for m in _SUBBATCH_RE.finditer(said):  # subbatch names
        end = m.end() + len(_sample_clause(said[m.end() :], ordered))
        blanked = _blank(blanked, m.end(), end)
    for pattern in (
        _SAMPLE_WORD_RE,
        _EXCEPT_RE,
    ):  # sample names, not a value after them
        for m in pattern.finditer(said):
            clause = _sample_clause(said[m.end() :], ordered)
            for item in _listed(clause, ordered):
                if item.hits:
                    blanked = _blank(blanked, m.end() + item.start, m.end() + item.end)
    names = [target.column.header, target.column.section.label]
    if anchor:
        names += [anchor.column.header, anchor.column.section.label]
    words = {w.lower() for w in re.findall(r'[A-Za-z]{3,}', ' '.join(names))}
    words |= _STEP_WORDS  # 'step 2 set the speed to 4000'
    indexes = {Decimal(d) for d in re.findall(r'\d+', ' '.join(names))}
    out = []
    for n in numbers(blanked):
        before = re.findall(r'[A-Za-z]+', blanked[: n.start])
        after = re.findall(r'[A-Za-z]+', blanked[n.end :])
        neighbours = {w.lower() for w in before[-1:] + after[:1]}
        if n.unit is None and n.value in indexes and neighbours & words:
            continue  # a number with a unit is a value ('rate 2 angstrom per second')
        out.append(n)
    return out


def _blank(text: str, start: int, end: int) -> str:
    return text[:start] + ' ' * (end - start) + text[end:]


def in_words(text: str, said: str) -> bool:
    """Text as written, ignoring case, spaces and hyphens, as a whole word:
    'Me 4PACz' is 'Me-4PACz'; '15min' is '15 MIN' but not '15 minutes'."""
    pattern = word_pattern(text)
    return pattern is not None and pattern.search(said) is not None


def word_pattern(text: str) -> re.Pattern | None:
    """The pattern `in_words` matches with; None for text without
    characters ('', ' -')."""
    chars = [re.escape(ch) for ch in text if ch not in ' -']
    if not chars:
        return None
    pattern = r'(?<![A-Za-z0-9])' + r'[\s-]*'.join(chars) + r'(?![A-Za-z0-9])'
    return re.compile(pattern, re.IGNORECASE)


# --- T type and U unit -------------------------------------------------------


def typed_value(new: str, target: ColumnInfo) -> tuple[object, list[str]]:
    """`new` in the type the column holds, ready to write: '4000 rpm' into
    a number column is 4000 (after its unit was checked against the
    header, never converted); text stays text; '' clears. Returns (value,
    problems)."""
    try:
        return _typed(new.strip(), target), []
    except Refusal as why:
        return None, [str(why)]


def _typed(new: str, target: ColumnInfo) -> object:
    name = column_name(target)
    if new == EMPTY:  # the LLM copied the table's marker for an empty cell
        raise Refusal(
            f'{name}: {new!r} is how the table shows an empty cell, not a value'
        )
    if new == '':
        return None
    if target.type == 'bool':
        if new.upper() not in ('TRUE', 'YES', 'FALSE', 'NO'):
            raise Refusal(f'{name} holds TRUE/FALSE; {new!r} is neither')
        return new.upper() in ('TRUE', 'YES')
    if target.type == 'date':
        raise Refusal(f'{name} holds dates; sand does not edit dates yet')
    if target.type == 'text':
        return new
    number = value(new)
    if number is None:
        if target.type == 'yyyymmdd':
            raise Refusal(f'{name} holds dates written as 20250101; {new!r} is not one')
        if target.type in ('int', 'float'):
            raise Refusal(f'{name} holds numbers; {new!r} is not a number')
        return new  # an empty or mixed column keeps what was said
    try:
        check_unit(number, target.unit)  # U
    except UnitMismatch as why:
        raise Refusal(str(why)) from None
    plain = (
        int(number.value)
        if number.value == number.value.to_integral_value()
        else float(number.value)
    )
    if target.type == 'yyyymmdd' and not (
        isinstance(plain, int) and is_day(str(plain))
    ):
        raise Refusal(f'{name} holds dates written as 20250101; {new!r} is not one')
    return plain


# --- values ------------------------------------------------------------------


def same_value(new: str, stored) -> bool:
    """Is the text the user gave the same as what the cell holds? Numbers
    compare as numbers ('3000' is 3000.0), text exactly, TRUE/FALSE as
    words, dates as ISO text ('2024-03-05'), empty only to empty."""
    given = new.strip()
    if stored is None or (isinstance(stored, str) and not stored.strip()):
        return given == ''
    if isinstance(stored, bool):
        return given.upper() == ('TRUE' if stored else 'FALSE')
    if isinstance(stored, int | float):
        n = value(new)
        return n is not None and n.value == Decimal(str(stored))
    if isinstance(stored, str):
        return given == stored.strip()
    return given in _date_forms(stored)


def _date_forms(stored) -> set[str]:
    """ISO spellings of a date or time cell. Excel keeps a date as a
    midnight datetime, so '2024-03-05' must match it too."""
    if isinstance(stored, datetime.datetime) and stored.time() == datetime.time():
        return {stored.isoformat(), stored.date().isoformat()}
    if isinstance(stored, datetime.date | datetime.time):
        return {stored.isoformat()}
    return set()


# --- helpers -----------------------------------------------------------------


def column_name(column: ColumnInfo) -> str:
    """How a column is named in messages: 'Rotation speed [rpm] (2: Spin Coating)'."""
    return f'{column.column.header} ({column.column.section.label})'


def _read_only(target: ColumnInfo, catalog: Catalog) -> str:
    """The message for a formula column, naming the columns to edit instead."""
    inputs = [catalog.column(i) for i in target.inputs or ()]
    names = ', '.join(c.column.header for c in inputs if c)
    hint = f' from {names}; edit {names} instead' if names else ''
    return f'{column_name(target)} is calculated by a formula{hint}'


def _text(v) -> str:
    return '' if v is None else str(v)
