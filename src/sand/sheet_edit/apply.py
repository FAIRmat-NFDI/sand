"""Run the pipeline: one instruction against one sheet, or every input in
order with the confirmed edits written.

The working file is never patched in place. It is rebuilt from the
untouched original every time:

    state_0 = original
    state_k = the confirmed edits of input k written into state_{k-1}
    final   = state_n

Input k is interpreted against state_{k-1}, so "the volume of the DMSO"
sees a DMSO an earlier input set, and only once that edit is confirmed.
A confirmation is bound to the input's text, the state it was given and
the edit itself; a rerun that produces the identical edit keeps it, any
difference asks again.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace

from sand.sheet_edit.catalog import Catalog, build
from sand.sheet_edit.coverage import Coverage, coverage
from sand.sheet_edit.edits import ProposedEdits
from sand.sheet_edit.ground import Grounded, Refused, ground
from sand.sheet_edit.preview import preview
from sand.sheet_edit.workbook import CellChange
from sand.sheet_edit.write import Write, WriteRefused, write_cells

# the LLM call: the instruction and the catalog of the sheet it is about
Resolve = Callable[[str, Catalog], ProposedEdits]
# (text hash, state hash) -> what the LLM proposed; only the LLM call is
# cached, the checks after it are cheap and always rerun
Cache = dict[tuple[str, str], ProposedEdits]


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
    sheet after it."""

    text: str
    interpretation: Interpretation
    written: tuple[CellChange, ...]  # recomputed formula results included
    refused: str | None  # the writer refused the confirmed edits: state unchanged
    state: bytes


def interpret_instruction(text: str, sheet: bytes, resolve: Resolve) -> Interpretation:
    """One instruction against one version of the sheet. Nothing is written.

    1. Build the catalog from the sheet bytes.
    2. Ask the LLM what the instruction means. It answers with edits.
    3. Ground each edit: does the cell exist, does the old value match,
       is the new value in the words.
    4. Coverage: did any part of the instruction get dropped.
    5. Preview: write the lines the user reads.
    """
    catalog = build(sheet)
    proposal = resolve(text, catalog)
    results = [ground(edit, catalog) for edit in proposal.edits]
    found = coverage(text, proposal, catalog)
    # an edit quoting words the user did not say is refused here, not by
    # ground, which sees the edit alone; the preview then shows it once
    results = [
        Refused(r.edit, (f'"{r.edit.said}" is not in your instruction',))
        if r.edit.said in found.misquoted
        else r
        for r in results
    ]
    state = _hash(sheet)
    ready = {
        _key(text, state, r): r for r in results if isinstance(r, Grounded) and r.rows
    }
    shown = preview(results, proposal.unresolved, replace(found, misquoted=()), catalog)
    return Interpretation(catalog, proposal, tuple(results), found, shown, ready)


def apply_all(
    original: bytes,
    inputs: list[str],
    resolve: Resolve,
    confirmed: set[str],
    cache: Cache,
) -> list[Step]:
    """Every input in order, each against the sheet the earlier ones
    left; the confirmed edits of each are written and verified. The last
    step's state is the final sheet. `cache` is filled as it goes."""
    steps = []
    state = original
    for text in inputs:
        cached = _cached(resolve, cache, _hash(state))
        interpretation = interpret_instruction(text, state, cached)
        writes = [
            Write(row, _column_index(edit, interpretation.catalog), edit.value)
            for key, edit in interpretation.ready.items()
            if key in confirmed
            for row in edit.rows
        ]
        written: tuple[CellChange, ...] = ()
        refused = None
        if writes:
            try:
                result = write_cells(state, writes)
                written, state = tuple(result.changes), result.xlsx
            except WriteRefused as why:
                refused = str(why)
        steps.append(Step(text, interpretation, written, refused, state))
    return steps


def _cached(resolve: Resolve, cache: Cache, state: str) -> Resolve:
    def cached(text: str, catalog: Catalog) -> ProposedEdits:
        key = (_hash(text.encode()), state)
        if key not in cache:
            cache[key] = resolve(text, catalog)
        return cache[key]

    return cached


def _key(text: str, state: str, edit: Grounded) -> str:
    """The confirmation key: the same words, on the same sheet, giving the
    same change."""
    change = [edit.edit.target, list(edit.rows), repr(edit.value)]
    return _hash(json.dumps([text, state, change]).encode())


def _column_index(edit: Grounded, catalog: Catalog) -> int:
    column = catalog.column(edit.edit.target)
    assert column is not None  # ground refused the edit otherwise
    return column.column.index


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
