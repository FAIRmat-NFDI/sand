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
read on: after an input above is reworded, or a rejection there taken
back, it is held until the inputs above are decided. A confirmation is
bound to the input's text, the sheet it was read on and the edit
itself; a rerun that produces the identical edit keeps it, any
difference asks again.

An answer read on one sheet is reused on another while what it looked
at there is the same (`lookup`, reads.py); else the model reads the
input again.

The inputs are decided in order. With `in_turn`, an input after the
first one with an undecided edit is not read again: its turn comes
after the inputs above are decided, and they may change its sheet once
more. It shows its last answer, checked against today's sheet, writes
nothing and adds nothing to the preview (`waiting`).
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from sand.sheet_edit.catalog import Catalog, build
from sand.sheet_edit.coverage import Coverage, coverage
from sand.sheet_edit.edits import ProposedEdits
from sand.sheet_edit.ground import Grounded, Refused, column_name, ground, read_old
from sand.sheet_edit.preview import preview
from sand.sheet_edit.reads import Reads, holds, reads_of, why_not
from sand.sheet_edit.workbook import CellChange, sha256
from sand.sheet_edit.write import Write, WriteRefused, write_cells

# the LLM call: the instruction and the catalog of the sheet it is about
Resolve = Callable[[str, Catalog], ProposedEdits]
CacheKey = tuple[str, str]  # (sha256 of the words, sha256 of the sheet read)


@dataclass(frozen=True)
class Answer:
    """What the model answered for some words on one sheet, and what code
    saved with it there. Only the LLM call is cached; the checks after
    it are cheap and always rerun."""

    proposal: ProposedEdits
    # each edit's `old` checked on the sheet read (`ground.read_old`);
    # None: not saved
    old_read: tuple[tuple[str, ...], ...] | None = None
    reads: Reads | None = None  # None: holds on its own sheet only
    seq: int = 0  # a newer answer has a larger one


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
    cache: Cache, text: str, sheet_hash: str, catalog: Catalog
) -> tuple[CacheKey, Answer] | None:
    """The answer for these words on this sheet: the one read on it, else
    the newest whose reads hold here."""
    words = sha256(text.encode())
    exact = cache.get((words, sheet_hash))
    if exact is not None:
        return (words, sheet_hash), exact
    for key in _newest_first(cache, words):
        answer = cache[key]
        if answer.reads is not None and holds(
            answer.reads, text, answer.proposal, catalog
        ):
            return key, answer
    return None


@dataclass(frozen=True)
class Interpretation:
    """What one instruction means on one version of the sheet."""

    catalog: Catalog  # of the sheet it was given
    proposal: ProposedEdits
    results: tuple[Grounded | Refused, ...]  # one per proposed edit, in order
    coverage: Coverage
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
    # an earlier answer shown until its turn: nothing written, not decidable
    waiting: bool = False
    why: str | None = None  # why that answer does not hold here (reads.py)
    answer: CacheKey | None = None  # the cache entry it used
    # its confirmed keys: a key two inputs share is the first one's
    confirmed: tuple[str, ...] = ()
    # confirmed, not written: read on a preview the written sheet is not
    held: tuple[str, ...] = ()


def interpret_instruction(
    text: str,
    sheet: bytes,
    resolve: Resolve,
    old_read: Sequence[Sequence[str]] | None = None,
    catalog: Catalog | None = None,
) -> Interpretation:
    """One instruction against one version of the sheet. Nothing is written.
    `old_read`: for an answer read on another sheet (`ground`); `catalog`:
    the sheet's, when already built.

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
    state = sha256(sheet)
    ready = {
        _key(text, state, r): r for r in results if isinstance(r, Grounded) and r.rows
    }
    # a misquoted edit is shown as refused above; a misquoted unresolved
    # item has no other line, so its coverage line stays
    quoted = {edit.said for edit in proposal.edits}
    shown_coverage = replace(
        found, misquoted=tuple(m for m in found.misquoted if m not in quoted)
    )
    shown = preview(results, proposal.unresolved, shown_coverage, catalog)
    return Interpretation(catalog, proposal, tuple(results), found, shown, ready)


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
    in_turn: bool = False,
    rejected: frozenset[str] = frozenset(),
) -> list[Step]:
    """Every input in order, each against the sheet the earlier ones
    left; the confirmed edits of each are written and verified. The last
    step's state is the final sheet. `cache` is filled as it goes. A
    writer refusal is recorded on the step; a failed verification is a
    bug in the writer and raises, since the file must not be used.
    `cached_only` stops before the first input with no answer here (the
    inputs after it would read a sheet it may change). `in_turn`: see
    the module docstring; `rejected` are the keys decided without a
    confirmation."""
    steps: list[Step] = []
    preview = written = original
    taken: set[str] = set()  # confirmed keys an earlier input holds
    for text in inputs:
        digest = sha256(preview)
        catalog = build(preview)
        found = lookup(cache, text, digest, catalog)
        if (
            found is None
            and in_turn
            and waits(steps, text, cache, confirmed=confirmed, rejected=rejected)
        ):
            last = _newest_first(cache, sha256(text.encode()))[0]
            earlier = cache[last]
            interpretation = interpret_instruction(
                text, preview, _answering(earlier), _off_sheet(earlier), catalog
            )
            why = why_not(earlier.reads, text, earlier.proposal, catalog)
            steps.append(
                Step(
                    text,
                    interpretation,
                    (),
                    None,
                    written,
                    preview,
                    waiting=True,
                    why=why,
                    answer=last,
                )
            )
            continue
        if found is None and cached_only:
            break
        if found is None:
            proposal = resolve(text, catalog)
            key = (sha256(text.encode()), digest)
            cache[key] = saved_answer(text, proposal, catalog, digest, next_seq(cache))
            found = key, cache[key]
        key, answer = found
        # read on this sheet: `old` is checked live
        old = None if key[1] == digest else _off_sheet(answer)
        interpretation = interpret_instruction(
            text, preview, _answering(answer), old, catalog
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


def waits(
    steps: list[Step],
    text: str,
    cache: Cache,
    *,
    confirmed: set[str],
    rejected: frozenset[str],
) -> bool:
    """Whether the next input, with no answer on this sheet (`lookup`),
    waits for its turn: an input above has an undecided edit, and an
    earlier answer can be shown. A new input is read once, on the sheet
    it is added to."""
    if not _newest_first(cache, sha256(text.encode())):
        return False
    return any(
        step.waiting
        or any(
            k not in confirmed and k not in rejected for k in step.interpretation.ready
        )
        for step in steps
    )


def _newest_first(cache: Cache, words: str) -> list[CacheKey]:
    """The answers to these words; an answer saved before `seq` existed
    goes by its place in the cache."""
    keys = [(i, k) for i, k in enumerate(cache) if k[0] == words]
    return [
        k
        for _, k in sorted(keys, key=lambda ik: (cache[ik[1]].seq, ik[0]), reverse=True)
    ]


def _answering(answer: Answer) -> Resolve:
    return lambda _text, _catalog: answer.proposal


def _off_sheet(answer: Answer) -> tuple[tuple[str, ...], ...]:
    """The saved `old` check, for the answer on another sheet; none saved:
    no `old` check there."""
    if answer.old_read is not None:
        return answer.old_read
    return tuple(() for _ in answer.proposal.edits)


def _key(text: str, state: str, edit: Grounded) -> str:
    """The confirmation key: the same words, on the same sheet, giving the
    same change."""
    change = [edit.edit.target, list(edit.rows), repr(edit.value)]
    return sha256(json.dumps([text, state, change]).encode())
