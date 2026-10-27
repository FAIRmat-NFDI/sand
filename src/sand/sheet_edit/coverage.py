"""Nothing the user said is silently dropped.

Ground checks the edits the LLM returned; coverage checks what it left
out. Every number in the instruction, and every word the sheet knows
(a step, a header, a value a cell holds), must lie in the words some edit
or unresolved item quoted and be used by it. What is left is listed as
not applied, as the clause it sits in:

    "solvent 2 to DMSO for samples 1 to 3, speed 4000, and anneal at 120"
    edits: Solvent 2 -> DMSO on samples 1-3; Rotation speed -> 4000
    -> not_applied: ('anneal at 120',)

Code only. The LLM is never asked what it covered; the instruction and
the edits' own fields decide.
"""

import re
from dataclasses import dataclass, replace

from sand.sheet_edit.catalog import Catalog, ColumnInfo
from sand.sheet_edit.edits import Answer, Edit, Range, Samples, Where
from sand.sheet_edit.ground import value_numbers, word_pattern
from sand.sheet_edit.numbers import Number, numbers, same_unit, value

# a clause ends at punctuation or at one of these words:
# "speed 4000, time 40", "speed 4000 and time 40"
_SPLIT_RE = re.compile(r'[,;.!?](?=\s|$)|\n|\b(?:and|then|also)\b', re.IGNORECASE)
_WORD_RE = re.compile(r'[A-Za-z]{3,}')
_PREFIX = 4  # 'anneal' names 'Annealing'; 'tem' names nothing
# header words said about the sheet, not to name a cell: "for all
# samples", "note: ..."
_GENERIC = {'sample', 'subbatch', 'experiment', 'info', 'notes'}
_MIN_VALUE_LENGTH = 2  # 'A' is a letter, not the value A

Span = tuple[int, int]


@dataclass(frozen=True)
class Coverage:
    """What the instruction says that nothing used.

    Coverage(
        not_applied=('anneal at 120',),  # the clause with the dropped number
        misquoted=('speed to 4000',),    # an edit's `said` not in the instruction
    )
    """

    not_applied: tuple[str, ...]
    misquoted: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not (self.not_applied or self.misquoted)


@dataclass(frozen=True)
class _Quoted:
    """The words one edit or unresolved item quoted, located in the
    instruction, and what it uses from them."""

    start: int
    end: int
    # numbers in the words that could be a value (so not sample numbers,
    # not the 2 of 'solvent 2'); each must be used by a text below
    values: tuple[Number, ...]
    # new, old, the anchor's and the rows' words, each with the unit of
    # its column (new '40' into Time [s] is 40 s, so '40 degrees' is not
    # it); an unresolved item: all its words, since the user sees it reported
    texts: tuple[tuple[str, str | None], ...]

    def holds(self, start: int, end: int) -> bool:
        return self.start <= start and end <= self.end


def coverage(instruction: str, answer: Answer, catalog: Catalog) -> Coverage:
    """Every number and every sheet word in the instruction must be quoted
    by an edit or unresolved item and used by it. The rest is listed as
    not applied, by clause:

        "speed 4000 and time 40"  with one edit, Rotation speed -> 4000
        -> not_applied: ('time 40',)
    """
    quoted, misquoted = _quoted(instruction, answer, catalog)
    runs = _uncovered_runs(instruction, quoted)
    flagged: list[tuple[Span, Span]] = []  # (what, the region to report it in)
    used: list[Span] = []  # numbers and values an edit uses: they end a report
    for n in numbers(instruction):
        region = _number_region(n, quoted, runs)
        _record((n.start, n.end), region, flagged, used)
    for pattern in _sheet_values(catalog).values():
        for m in pattern.finditer(instruction):
            region = _value_region(pattern, m.span(), quoted, runs)
            _record(m.span(), region, flagged, used)
    vocabulary = _vocabulary(catalog)
    for m in _WORD_RE.finditer(instruction):
        run = _run_at(*m.span(), runs)
        if run is not None and _known(m.group(), vocabulary):
            flagged.append((m.span(), run))
    clauses = _clauses(instruction)
    not_applied: list[str] = []
    for what, region in sorted(flagged):
        text = _report(instruction, clauses, what, region, used)
        if text and text not in not_applied:
            not_applied.append(text)
    return Coverage(tuple(not_applied), tuple(misquoted))


def _record(
    what: Span, region: Span | None, flagged: list[tuple[Span, Span]], used: list[Span]
) -> None:
    """A number or value is used (no region to report it in) or flagged."""
    if region is None:
        used.append(what)
    else:
        flagged.append((what, region))


# --- what the edits quoted ---------------------------------------------------


def _quoted(
    instruction: str, answer: Answer, catalog: Catalog
) -> tuple[list[_Quoted], list[str]]:
    """Each edit's and unresolved item's `said` located in the instruction;
    a `said` that is not there is returned as misquoted and covers nothing."""
    quoted, misquoted = [], []
    for edit in answer.edits:
        spans = _occurrences(edit.said, instruction)
        quoted += [_edit_quoted(instruction, span, edit, catalog) for span in spans]
        if not spans:
            misquoted.append(edit.said)
    for item in answer.unresolved:
        spans = _occurrences(item.said, instruction)
        quoted += [_Quoted(a, b, (), ((instruction[a:b], None),)) for a, b in spans]
        if not spans:
            misquoted.append(item.said)
    return quoted, misquoted


def _occurrences(said: str, instruction: str) -> list[Span]:
    """Where `said` is in the instruction: the same whole words in the same
    order, ignoring case and how much space is between them ('time 40'
    is not in 'time 400'). A number cut by the quote's end is taken in
    whole: 'speed 4000' in 'speed 4000 rpm' is 'speed 4000 rpm'."""
    tokens = said.strip(' .,;:!?').split()
    if not tokens:
        return []
    pattern = r'\s+'.join(re.escape(t) for t in tokens)
    pattern = rf'(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])'
    found = numbers(instruction)
    spans = []
    for m in re.finditer(pattern, instruction, re.I):
        start, end = m.span()
        for n in found:
            if n.start < start < n.end:
                start = n.start
            if n.start < end < n.end:
                end = n.end
        spans.append((start, end))
    return spans


def _edit_quoted(instruction: str, span: Span, edit: Edit, catalog: Catalog) -> _Quoted:
    start, end = span
    words = instruction[start:end]
    target = catalog.column(edit.target)
    anchor = catalog.column(edit.anchor.column) if edit.anchor else None
    if target is None:  # not a column of this sheet: ground refuses the edit
        found = numbers(words)
    else:
        found = value_numbers(words, target, anchor, catalog)
    values = tuple(replace(n, start=n.start + start, end=n.end + start) for n in found)
    return _Quoted(start, end, values, tuple(_texts(edit, target, catalog)))


def _texts(
    edit: Edit, target: ColumnInfo | None, catalog: Catalog
) -> list[tuple[str, str | None]]:
    """The words of the edit's fields, what it uses from the instruction,
    each with its column's unit."""
    unit = target.unit if target else None
    texts = [(edit.new, unit), (edit.old or '', unit)]
    if edit.anchor:
        texts.append((edit.anchor.equals, None))
    rows = edit.rows
    if isinstance(rows, Samples):
        texts += [(t, None) for t in (*rows.samples, rows.subbatch or '')]
    elif isinstance(rows, Range):
        texts += [(t, None) for t in (rows.first, rows.last, rows.subbatch or '')]
    elif isinstance(rows, Where):
        column = catalog.column(rows.where)
        texts.append((rows.equals or '', column.unit if column else None))
    return [(t, u) for t, u in texts if t]


# --- what is not used ----------------------------------------------------------


def _number_region(n: Number, quoted: list[_Quoted], runs: list[Span]) -> Span | None:
    """The region to report for a number nothing uses; None when it is used.
    Inside quoted words a number is used when it is no value (sample 3,
    solvent 2: ground checks those) or a field holds it (new '4000',
    where '20min-hot' for '20min')."""
    holders = [q for q in quoted if q.holds(n.start, n.end)]
    for q in holders:
        if all(v.start != n.start for v in q.values):
            return None
        if any(_uses(text, unit, n) for text, unit in q.texts):
            return None
    if holders:
        return (holders[0].start, holders[0].end)
    return _run_at(n.start, n.end, runs) or (n.start, n.end)


def _uses(text: str, unit: str | None, n: Number) -> bool:
    """'4000' and '4000 rpm' use 'four thousand'; '20min-hot' uses '20min';
    '40' into a [s] column does not use '40 degrees'."""
    number = value(text)
    if number is not None:
        unit = number.unit or unit
        if unit and n.unit and not same_unit(n.unit, unit):
            return False
        return number.value == n.value
    pattern = word_pattern(n.text)
    return pattern is not None and pattern.search(text) is not None


def _value_region(
    pattern: re.Pattern, span: Span, quoted: list[_Quoted], runs: list[Span]
) -> Span | None:
    """The region to report for a sheet value written in the instruction
    ('DMSO', 'Ethanol wash') that no field holds; None when one does."""
    holders = [q for q in quoted if q.holds(*span)]
    if any(pattern.search(t) for q in holders for t, _ in q.texts):
        return None
    if holders:
        return (holders[0].start, holders[0].end)
    return _run_at(*span, runs) or span


def _sheet_values(catalog: Catalog) -> dict[str, re.Pattern]:
    """The texts the cells hold ('DMSO', 'Ethanol wash', a label, a Nomad
    ID), each with the pattern that finds it written in any case. Numbers
    are left to the number check."""
    texts = {
        str(group.value)
        for column in catalog.columns
        for group in column.groups
        if isinstance(group.value, str)
    }
    texts |= {s.label for s in catalog.samples if s.label}
    texts |= {s.nomad_id for s in catalog.samples if s.nomad_id}
    patterns = {t: word_pattern(t) for t in texts if len(t) >= _MIN_VALUE_LENGTH}
    return {t: p for t, p in patterns.items() if p is not None and value(t) is None}


def _vocabulary(catalog: Catalog) -> set[str]:
    """The words of the step names and headers: 'spin', 'coating', 'speed',
    'rpm'. Not the words of cell values: free text in Notes would make
    'ok' a sheet word. Values count whole, through `_sheet_values`."""
    texts = [c.column.header for c in catalog.columns]
    texts += [c.column.section.label for c in catalog.columns]
    words = {w.lower() for w in _WORD_RE.findall(' '.join(texts))}
    return words - _GENERIC


def _known(token: str, vocabulary: set[str]) -> bool:
    """'speed' is known; 'anneal' is, as the start of 'annealing';
    'times' is not (it only starts with 'time'); 'quickly' is not."""
    token = token.lower()
    if token in vocabulary:
        return True
    if len(token) < _PREFIX:
        return False
    return any(word.startswith(token) for word in vocabulary)


# --- where to say it -----------------------------------------------------------


def _uncovered_runs(instruction: str, quoted: list[_Quoted]) -> list[Span]:
    """The stretches of the instruction outside every quoted span."""
    covered = [False] * len(instruction)
    for q in quoted:
        for i in range(q.start, q.end):
            covered[i] = True
    runs, start = [], None
    for i, c in enumerate([*covered, True]):
        if not c and start is None:
            start = i
        if c and start is not None:
            runs.append((start, i))
            start = None
    return runs


def _run_at(start: int, end: int, runs: list[Span]) -> Span | None:
    return next((r for r in runs if r[0] <= start and end <= r[1]), None)


def _clauses(instruction: str) -> list[Span]:
    """The instruction cut at punctuation and 'and': "speed 4000, and time
    40" -> "speed 4000", "time 40". Not inside a spoken number or a list
    of samples: "samples 1, 3 and 7 to 9" stays one clause."""
    clauses, start = [], 0
    found = numbers(instruction)
    for m in _SPLIT_RE.finditer(instruction):
        if any(n.start <= m.start() and m.end() <= n.end for n in found):
            continue  # the 'and' of 'one hundred and twenty'
        before = instruction[: m.start()].split()[-1:]
        after = instruction[m.end() :].split()[:1]
        if before and after and _listed(before[0]) and _listed(after[0]):
            continue
        clauses.append((start, m.start()))
        start = m.end()
    clauses.append((start, len(instruction)))
    return clauses


def _listed(token: str) -> bool:
    """A sample list item: a bare number ('3' in 'samples 1, 3 and 7'), or
    the 'no' of 'sample no. 3'."""
    token = token.strip('.,;:')
    return token.lower() in ('no', 'nr') or any(n.unit is None for n in numbers(token))


def _report(
    instruction: str, clauses: list[Span], what: Span, region: Span, used: list[Span]
) -> str:
    """The words to show for `what`: its clause, cut to `region` (the quoted
    words or the uncovered stretch it was found in) and to the nearest
    used items. "speed 4000 rpm for 30 seconds", one edit using 4000 rpm
    -> "for 30 seconds"."""
    clause = next(c for c in clauses if c[0] <= what[0] < c[1])
    start = max([clause[0], region[0], *(u[1] for u in used if u[1] <= what[0])])
    end = min([clause[1], region[1], *(u[0] for u in used if u[0] >= what[1])])
    return instruction[start:end].strip(' \t\n,;.:!?')
