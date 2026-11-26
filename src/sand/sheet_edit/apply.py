"""Run the pipeline: one instruction against one sheet, or every input in
order with the confirmed edits written.

The working file is never patched in place. It is rebuilt from the
untouched original every time, as two sheets:

    preview_0 = written_0 = original
    input k is read on preview_{k-1}
    written_k = written_{k-1} + the confirmed edits of input k, when
                preview_{k-1} == written_{k-1}; else unchanged (held)
    preview_k = preview_{k-1} + its confirmed and undecided edits
    final     = written_n

So input k sees what the inputs above would leave if every edit still
waiting there is confirmed: "the volume of the DMSO" sees a DMSO an
earlier input proposes, and confirming that input leaves nothing to
redo below it. A confirmed edit is written only onto the sheet it was
read on: while an input above has an edit waiting for a decision, it is
held.

The page decides one input after the other, or all at once (the router
checks it); this module takes decisions in any order. A decision is bound to its input
and to what the user saw (`_key`): the words, and the edit with its old
values, samples and checks. A decision above that changes none of it below keeps the
decisions there; one that changes it (the old value, the rows, a new
check) leaves the new edit undecided.

An answer read on one sheet is reused on another while what it looked
at there is the same (`lookup`, reads.py); else the model reads the
input again.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from sand.sheet_edit.catalog import Catalog, build
from sand.sheet_edit.coverage import coverage
from sand.sheet_edit.edits import ProposedEdits
from sand.sheet_edit.ground import Grounded, Refused, column_name, ground, read_old
from sand.sheet_edit.preview import edit_summary, preview
from sand.sheet_edit.reads import Reads, holds, reads_of
from sand.sheet_edit.resolve import known_step
from sand.sheet_edit.workbook import CellChange, sha256
from sand.sheet_edit.write import Write, WriteRefused, write_cells

# the LLM call: the instruction and the catalog of the sheet it is about
Resolve = Callable[[str, Catalog], ProposedEdits]
CacheKey = tuple[str, str]  # (`words_key`, sha256 of the sheet read)


def words_key(text: str, step: str = '') -> str:
    """The words' part of a cache key. The step the note was made on is
    in it: the same words on two steps are two questions."""
    return sha256(f'{text}\0{step}'.encode() if step else text.encode())


@dataclass(frozen=True)
class Answer:
    """What the model answered for some words on one sheet, and what code
    saved with it there. Only the LLM call is cached; the checks after
    it are cheap and always rerun."""

    proposal: ProposedEdits
    # each edit's `old` checked on the sheet read (`ground.read_old`)
    old_read: tuple[tuple[str, ...], ...]
    reads: Reads
    seq: int  # a newer answer has a larger one


# The sheet is part of the key because the LLM reads its catalog: "the
# time of the DMSO" finds nothing on the original sheet and an anchored
# edit once an earlier input set DMSO. An answer is reused on another
# sheet while its `reads` hold there (reads.py).
Cache = dict[CacheKey, Answer]


def saved_answer(
    text: str, proposal: ProposedEdits, catalog: Catalog, sheet_hash: str, seq: int
) -> Answer:
    """The answer with what is saved on the sheet it was read on."""
    old = tuple(tuple(found) for found in read_old(proposal, catalog))
    return Answer(proposal, old, reads_of(text, proposal, catalog, sheet_hash), seq)


def next_seq(cache: Cache) -> int:
    return max((a.seq for a in cache.values()), default=0) + 1


def lookup(
    cache: Cache, text: str, sheet_hash: str, catalog: Catalog, step: str = ''
) -> tuple[CacheKey, Answer] | None:
    """The answer for these words (said on `step`) on this sheet: the one
    read on it, else the newest whose reads hold here."""
    words = words_key(text, step)
    exact = cache.get((words, sheet_hash))
    if exact is not None:
        return (words, sheet_hash), exact
    for key in _newest_first(cache, words):
        answer = cache[key]
        if holds(answer.reads, text, answer.proposal, catalog):
            return key, answer
    return None


@dataclass(frozen=True)
class Interpretation:
    """What one instruction means on one version of the sheet."""

    catalog: Catalog  # of the sheet it was given
    proposal: ProposedEdits
    results: tuple[Grounded | Refused, ...]  # one per proposed edit, in order
    preview: str  # what the user reads
    ready: dict[str, Grounded]  # edits that may be written, by confirmation key


@dataclass(frozen=True)
class Step:
    """One input of `apply_all`: what it meant, what was written, the
    two sheets after it."""

    text: str
    interpretation: Interpretation  # read on the preview before it
    written: tuple[CellChange, ...]  # recomputed formula results included
    refused: str | None  # the writer refused the confirmed edits: state unchanged
    state: bytes  # the written sheet: original plus confirmed edits
    # the sheet the next input is read on: `state` plus the edits waiting
    # for a decision
    preview: bytes
    answer: CacheKey | None = None  # the cache entry it used
    # its confirmed keys: a key two inputs share is the first one's
    confirmed: tuple[str, ...] = ()
    # confirmed, not written: read on a preview the written sheet is not
    held: tuple[str, ...] = ()


def interpret_instruction(  # noqa: PLR0913
    text: str,
    sheet: bytes,
    resolve: Resolve,
    old_read: Sequence[Sequence[str]] | None = None,
    catalog: Catalog | None = None,
    *,
    owner: str = '',
) -> Interpretation:
    """One instruction against one version of the sheet. Nothing is written.
    `old_read`: for an answer read on another sheet (`ground`); `catalog`:
    the sheet's, when already built; `owner`: the input's id, in its keys.

    1. Build the catalog from the sheet bytes.
    2. Ask the LLM what the instruction means. It answers with edits.
    3. Ground each edit: does the cell exist, does the old value match,
       is the new value in the words.
    4. Coverage: did any part of the instruction get dropped.
    5. Preview: write the lines the user reads.
    """
    catalog = catalog or build(sheet)
    proposal = resolve(text, catalog)
    results = [
        ground(edit, catalog, None if old_read is None else tuple(old_read[i]))
        for i, edit in enumerate(proposal.edits)
    ]
    found = coverage(text, proposal, catalog)
    # ground sees one edit alone; what needs the whole instruction is
    # refused here: an edit quoting words the user did not say, and an
    # edit writing a cell an earlier edit of the same instruction writes
    results = [
        Refused(r.edit, (f'"{r.edit.said}" is not in your instruction',))
        if r.edit.said in found.misquoted
        else r
        for r in results
    ]
    results = _without_overlaps(results, catalog)
    ready = {
        _key(owner, text, r, catalog): r
        for r in results
        if isinstance(r, Grounded) and r.rows
    }
    # a misquoted edit is shown as refused above; a misquoted unresolved
    # item has no other line, so its coverage line stays
    quoted = {edit.said for edit in proposal.edits}
    shown_coverage = replace(
        found, misquoted=tuple(m for m in found.misquoted if m not in quoted)
    )
    shown = preview(results, proposal.unresolved, shown_coverage, catalog)
    return Interpretation(catalog, proposal, tuple(results), shown, ready)


def _without_overlaps(
    results: list[Grounded | Refused], catalog: Catalog
) -> list[Grounded | Refused]:
    """The later of two edits writing the same cell is refused: "solvent 2
    to DMSO for samples 1 to 3 and to ETH for sample 2" cannot both be
    done, and the writer would refuse the whole step. An edit a word
    check marked comes after every clean one: it never blocks a clean
    edit of the same cell."""
    taken: dict[tuple[str, int], Grounded] = {}  # (target, row) -> the edit
    decided: dict[int, Grounded | Refused] = {}
    order = sorted(
        range(len(results)),
        key=lambda i: isinstance(results[i], Grounded) and bool(results[i].checks),
    )
    for i in order:
        r = results[i]
        if isinstance(r, Refused):
            decided[i] = r
            continue
        cells = [(r.edit.target, row) for row in r.rows]
        first = next((taken[c] for c in cells if c in taken), None)
        if first is None:
            taken.update(dict.fromkeys(cells, r))
            decided[i] = r
            continue
        rows = [row for target, row in cells if taken.get((target, row)) is first]
        column = catalog.column(r.edit.target)
        assert column is not None  # ground refused the edit otherwise
        why = (
            f'{column_name(column)} on samples {catalog.names(rows)} is already '
            f'changed by "{first.edit.said}"'
        )
        decided[i] = Refused(r.edit, (why,))
    return [decided[i] for i in range(len(results))]


def apply_all(  # noqa: PLR0913
    original: bytes,
    inputs: list[str],
    resolve: Resolve,
    confirmed: set[str],
    cache: Cache,
    *,
    cached_only: bool = False,
    rejected: frozenset[str] = frozenset(),
    ids: Sequence[str] | None = None,
    steps_of: Sequence[str] | None = None,
) -> list[Step]:
    """Every input in order, each against the sheet the earlier ones
    left; the confirmed edits of each are written and verified. The last
    step's state is the final sheet. `cache` is filled as it goes. A
    writer refusal is recorded on the step; a failed verification is a
    bug in the writer and raises, since the file must not be used.
    `cached_only` stops before the first input with no answer here (the
    inputs after it would read a sheet it may change). `rejected` are
    the keys decided without a confirmation; `ids` the inputs' own (their
    entry ids), so two inputs showing the same edit are decided apart.
    `steps_of` are the inputs' labels: one that is a step of the sheet is
    the step the note was made on. It is in the cache key only: `resolve`
    is not given it, the job asks the model itself (resolve.prompts)."""
    steps: list[Step] = []
    preview = written = original
    taken: set[str] = set()  # confirmed keys an earlier input holds
    for number, text in enumerate(inputs):
        owner = ids[number] if ids else ''
        digest = sha256(preview)
        catalog = build(preview)
        step = known_step(steps_of[number], catalog) if steps_of else ''
        found = lookup(cache, text, digest, catalog, step)
        if found is None and cached_only:
            break
        if found is None:
            proposal = resolve(text, catalog)
            key = (words_key(text, step), digest)
            cache[key] = saved_answer(text, proposal, catalog, digest, next_seq(cache))
            found = key, cache[key]
        key, answer = found
        # read on this sheet: `old` is checked live
        old = None if key[1] == digest else answer.old_read
        interpretation = interpret_instruction(
            text, preview, _answering(answer), old, catalog, owner=owner
        )
        ready = interpretation.ready
        # a key two inputs share is the first one's, as the server records
        # its confirmation; a rejection holds for every input with it
        mine = [k for k in ready if k in confirmed and k not in taken]
        taken.update(mine)
        kept = [k for k in ready if k in mine or k not in rejected]
        changes: tuple[CellChange, ...] = ()
        refused = None
        held: tuple[str, ...] = ()
        on_written = preview == written
        if mine and on_written:
            try:
                result = write_cells(written, _writes(interpretation, mine))
                changes, written = tuple(result.changes), result.xlsx
            except WriteRefused as why:
                refused = str(why)
        elif mine:
            # finding D: written here, the edit would land on a sheet the
            # user did not see it on
            held = tuple(mine)
        if on_written and len(kept) == len(mine):
            preview = written
        elif kept:
            preview = _previewed(
                preview, written, on_written, interpretation, kept, mine
            )
        steps.append(
            Step(
                text,
                interpretation,
                changes,
                refused,
                written,
                preview,
                answer=key,
                confirmed=tuple(mine),
                held=held,
            )
        )
    return steps


def _writes(interpretation: Interpretation, keys: list[str]) -> list[Write]:
    writes = []
    for k in keys:
        edit = interpretation.ready[k]
        column = interpretation.catalog.index(edit.edit.target)
        writes += [Write(row, column, edit.value) for row in edit.rows]
    return writes


def _previewed(  # noqa: PLR0913, PLR0917
    preview: bytes,
    written: bytes,
    on_written: bool,
    interpretation: Interpretation,
    kept: list[str],
    mine: list[str],
) -> bytes:
    """The preview after a step with edits waiting for a decision. A
    write the writer refuses falls back to the confirmed edits alone
    (finding N): else a confirmed edit could be in the written sheet and
    not in the preview."""
    # never downloaded: not read back. The bytes are the same as a
    # verified write's, so a preview and the written sheet stay comparable
    try:
        return write_cells(preview, _writes(interpretation, kept), verified=False).xlsx
    except WriteRefused:
        pass
    if on_written:
        return written  # its confirmed write is done, or was refused too
    if not mine:
        return preview
    try:
        return write_cells(preview, _writes(interpretation, mine), verified=False).xlsx
    except WriteRefused:
        return preview


def _newest_first(cache: Cache, words: str) -> list[CacheKey]:
    keys = [k for k in cache if k[0] == words]
    return sorted(keys, key=lambda k: cache[k].seq, reverse=True)


def _answering(answer: Answer) -> Resolve:
    return lambda _text, _catalog: answer.proposal


def _key(owner: str, text: str, edit: Grounded, catalog: Catalog) -> str:
    """The decision key: the input's, the same words giving the same
    change, as the user saw it (old values, samples, checks). Not the
    sheet: a change above that leaves all of it the same keeps the
    decision."""
    change = [edit.edit.target, list(edit.rows), repr(edit.value)]
    seen = [edit_summary(edit, catalog), list(edit.checks)]
    return sha256(json.dumps([owner, text, change, seen]).encode())
