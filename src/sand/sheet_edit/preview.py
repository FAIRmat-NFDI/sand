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
    ⚠ no update in: "time 40"

The first character says what to do: nothing (fine, or a · to glance at),
? (confirm one by one), ✗ or ⚠ (rephrase), = (nothing to change).
"""

from collections.abc import Iterable

from sand.sheet_edit.catalog import Catalog, ColumnInfo
from sand.sheet_edit.coverage import Coverage
from sand.sheet_edit.edits import (
    Ambiguous,
    Edit,
    Range,
    Samples,
    Uncertain,
    Unreadable,
    Unresolved,
)
from sand.sheet_edit.ground import (
    Grounded,
    Refusal,
    Refused,
    column_name,
    resolve_rows,
    samples_text,
)
from sand.sheet_edit.render import EMPTY, value_text

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
    lines += [f'⚠ no update in: "{clause}"' for clause in coverage.not_applied]
    lines += [f'⚠ misquoted: "{said}"' for said in coverage.misquoted]
    return '\n'.join(lines) or NOTHING


def edit_lines(result: Grounded | Refused, catalog: Catalog) -> list[str]:
    """One edit: what changes where, its words, then each note (·), check
    (⚠) and doubt (?). A refused edit: its words and each reason."""
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
    lines += _indented(f'⚠ check: {_short(c, target)}' for c in result.checks)
    lines += _indented(f'? {_short(doubt, target)}' for doubt in result.doubts)
    return lines


def edit_summary(result: Grounded, catalog: Catalog) -> dict[str, str]:
    """The edit's parts for the GUI: step, column, old and new value, the
    samples. A no-op (no rows to write) has old == new."""
    target = catalog.column(result.edit.target)
    assert target is not None  # ground refused it otherwise
    new = value_text(result.value)
    old = _old_text(result, target) if result.rows else new
    # the samples left out because they hold the value already: said at
    # the edit, so nobody wonders why sample 1 is not in the list
    unchanged = ''
    if result.rows and result.unchanged:
        kept = samples_text(result.unchanged, catalog)
        unchanged = f'{kept} already {result.edit.new or "empty"}'
    return {
        'step': target.column.section.label,
        'column': target.column.header,
        'old': old,
        'new': new,
        'samples': samples_text(result.rows or result.unchanged, catalog),
        'unchanged': unchanged,
    }


def _change(result: Grounded, target: ColumnInfo, catalog: Catalog) -> str:
    """'3000 → 4000, all 12 samples'; 'various → 4000, …' when the rows
    written hold different values (the note under it says which)."""
    old = _old_text(result, target)
    return f'{old} → {value_text(result.value)}, {samples_text(result.rows, catalog)}'


def _old_text(result: Grounded, target: ColumnInfo) -> str:
    held = {r: g.value for g in target.groups for r in g.rows}
    olds = list(dict.fromkeys(value_text(held.get(r)) for r in result.rows))
    return olds[0] if len(olds) == 1 else 'various'


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
    if isinstance(item, Uncertain):
        readings = [
            f'{INDENT}{"or " if i else ""}{_reading(edit, catalog)}'
            for i, edit in enumerate(item.readings)
        ]
        return [f'? uncertain: "{item.said}"', *readings, f'{INDENT}{item.why}']
    if isinstance(item, Unreadable):
        return [f"? not done, the model's answer could not be read: {item.detail}"]
    return [f'? not found: "{item.said}"']


def _reading(edit: Edit, catalog: Catalog) -> str:
    """'2: Spin Coating · Rotation speed [rpm] → 4000, samples 1–3 of
    subbatch 1, pointed through Solvent 2 = DMSO': everything two
    readings may differ in, or the user cannot tell them apart."""
    column = catalog.column(edit.target)
    where = _where(column) if column else edit.target
    rows = edit.rows
    subbatch = None
    if rows == 'all':
        samples = f'all samples ({_written(rows, catalog)})'
    elif isinstance(rows, Samples):
        samples, subbatch = f'samples {", ".join(rows.samples)}', rows.subbatch
    elif isinstance(rows, Range):
        samples, subbatch = f'samples {rows.first}–{rows.last}', rows.subbatch
    else:
        held = catalog.column(rows.where)
        name = _where(held) if held else rows.where
        samples = f'where {name} ' + (
            'is empty' if rows.equals is None else f'= {rows.equals}'
        )
        samples += f' ({_written(rows, catalog)})'
    text = f'{where} → {edit.new or EMPTY}, {samples}'
    if subbatch is not None:
        text += f' of subbatch {subbatch}'
    if edit.anchor:
        through = catalog.column(edit.anchor.column)
        name = through.column.header if through else edit.anchor.column
        text += f', pointed through {name} = {edit.anchor.equals}'
    if edit.said_old and edit.said_old.strip():
        text += f', you said it was {edit.said_old.strip()}'
    return text


def _written(rows, catalog: Catalog) -> str:
    """The samples 'all' or a condition stands for, so that two readings
    that differ there are told apart: '1–5, 7–13' vs 'samples 3, 7'."""
    try:
        found, _ = resolve_rows(rows, catalog)
    except Refusal:
        return 'no sample'
    names = catalog.names(found)
    return names if rows == 'all' else f'samples {names}'


def _short(message: str, target: ColumnInfo) -> str:
    """A ground message without the column it names, which the first line
    already does: 'Rotation speed [rpm] (2: Spin Coating): read from
    ...' -> 'read from ...'."""
    return message.removeprefix(f'{column_name(target)}: ')


def _where(column: ColumnInfo) -> str:
    return f'{column.column.section.label} · {column.column.header}'


def _indented(lines: Iterable[str]) -> list[str]:
    return [f'{INDENT}{line}' for line in lines]
