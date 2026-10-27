"""The plain text the user confirms before anything is written.

Built by code from the catalog, never by the LLM: the real step and
header, the real old value, the samples listed, and the user's own words
next to each line. One instruction's whole outcome reads as

    2: Spin Coating · Rotation speed [rpm]: 3000 → 4000, all 12 samples
       from "speed four thousand"
       · read from 'four thousand'
    ✗ not applied: "speed 4100"
       Rotation speed [rpm] (2: Spin Coating): '4100' is not in your words; you said 4000
    ? not found: "anneal at 120"
    ⚠ not applied: "time 40"

The first character says what to do: nothing (fine, or a · to glance at),
? (confirm one by one), ✗ or ⚠ (rephrase), = (nothing to change).
"""

from collections.abc import Iterable

from sand.sheet_edit.catalog import Catalog, ColumnInfo
from sand.sheet_edit.coverage import Coverage
from sand.sheet_edit.edits import Ambiguous, Unresolved
from sand.sheet_edit.ground import Grounded, Refused, column_name
from sand.sheet_edit.render import value_text

INDENT = '   '
NOTHING = 'nothing to edit'


def preview(
    results: list[Grounded | Refused],
    unresolved: list[Unresolved],
    coverage: Coverage,
    catalog: Catalog,
) -> str:
    """One instruction's outcome as lines: the edits in order, then what
    the LLM could not place, then what nothing used."""
    lines = [line for result in results for line in edit_lines(result, catalog)]
    for item in unresolved:
        lines += _unresolved_lines(item, catalog)
    lines += [f'⚠ not applied: "{clause}"' for clause in coverage.not_applied]
    lines += [f'⚠ misquoted: "{said}"' for said in coverage.misquoted]
    return '\n'.join(lines) or NOTHING


def edit_lines(result: Grounded | Refused, catalog: Catalog) -> list[str]:
    """One edit: what changes where, its words, then each note (·) and
    doubt (?). A refused edit: its words and each reason."""
    if isinstance(result, Refused):
        return [f'✗ not applied: "{result.edit.said}"', *_indented(result.reasons)]
    target = catalog.column(result.edit.target)
    assert target is not None  # ground refused it otherwise
    if result.status == 'noop':
        head = f'= {_where(target)}'
    else:
        head = f'{_where(target)}: {_change(result, target, catalog)}'
    lines = [head, f'{INDENT}from "{result.edit.said}"']
    lines += _indented(f'· {_short(note, target)}' for note in result.notes)
    if result.rows:  # a no-op changes nothing, so no formula changes either
        lines += _indented(f'· {mark}' for mark in _marks(result, target, catalog))
    lines += _indented(f'? {_short(doubt, target)}' for doubt in result.doubts)
    return lines


def _change(result: Grounded, target: ColumnInfo, catalog: Catalog) -> str:
    """'3000 → 4000, all 12 samples'. The rows written all hold the same
    value: check O refused the edit otherwise."""
    held = {r: g.value for g in target.groups for r in g.rows}
    old = value_text(held.get(result.rows[0]))
    return f'{old} → {value_text(result.value)}, {_samples(result.rows, catalog)}'


def _samples(rows: tuple[int, ...], catalog: Catalog) -> str:
    if len(rows) == len(catalog.samples):
        return f'all {len(rows)} samples'
    return f'samples {catalog.names(rows)}'


def _marks(result: Grounded, target: ColumnInfo, catalog: Catalog) -> list[str]:
    """What to look at twice: a text no cell holds yet (a misheard material
    name is the likeliest voice error), and formulas that read the column."""
    marks = []
    if isinstance(result.value, str) and result.value.strip() not in _texts(catalog):
        marks.append(f'new term: {result.value}')
    readers = [c for c in catalog.columns if target.id in (c.inputs or ())]
    if readers:
        names = ', '.join(_where(c) for c in readers)
        marks.append(f'changes {names} by formula')
    return marks


def _texts(catalog: Catalog) -> set[str]:
    return {
        g.value.strip()
        for c in catalog.columns
        for g in c.groups
        if isinstance(g.value, str)
    }


def _unresolved_lines(item: Unresolved, catalog: Catalog) -> list[str]:
    if isinstance(item, Ambiguous):
        columns = [catalog.column(c) for c in item.candidates]
        names = [_where(c) if c else cid for c, cid in zip(columns, item.candidates)]
        return [f'? ambiguous: "{item.said}"', f'{INDENT}{" or ".join(names)}?']
    return [f'? not found: "{item.said}"']


def _short(message: str, target: ColumnInfo) -> str:
    """A ground message without the column it names, which the first line
    already does: 'Rotation speed [rpm] (2: Spin Coating): read from
    ...' -> 'read from ...'."""
    return message.removeprefix(f'{column_name(target)}: ')


def _where(column: ColumnInfo) -> str:
    return f'{column.column.section.label} · {column.column.header}'


def _indented(lines: Iterable[str]) -> list[str]:
    return [f'{INDENT}{line}' for line in lines]
