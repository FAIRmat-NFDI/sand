"""Where an answer looked in the sheet: the values the note names and
the cells holding them. An answer read on one sheet holds on another
while these are the same there; the model is then not asked again.

Named values: the anchor, `where` and `said_old` values of the edits,
every text value of the sheet found in the words, and the numbers of
the words that are not an edit's new value (the 120 of "the anneal at
120 °C"). They are looked for in every column: "the DMSO" read while
one column holds DMSO is ambiguous once a second does. The cells an
edit writes that were empty must still be empty: the model may have
picked that column because it was empty ("add DMSO as another
solvent").

Some answers hold only on the sheet they were read on (`whole`): a part
the model could not place or left out, a new value not in the words
(computed or copied from other cells), an anchor value not in the words.
"""

import json
from dataclasses import dataclass

from sand.sheet_edit.catalog import Catalog
from sand.sheet_edit.coverage import coverage, sheet_values
from sand.sheet_edit.edits import ProposedEdits, Where
from sand.sheet_edit.ground import (
    Refusal,
    column_name,
    in_words,
    resolve_rows,
    same_value,
    value_numbers,
    value_problems,
)
from sand.sheet_edit.numbers import value
from sand.sheet_edit.workbook import sha256

Cell = tuple[str, int]  # (column id, row)


@dataclass(frozen=True)
class Reads:
    whole: str | None  # set: holds only on the sheet with this hash
    structure: str  # hash of the columns and the samples
    held: tuple[tuple[str, tuple[Cell, ...]], ...]  # named value -> its cells
    blank: tuple[tuple[str, tuple[int, ...]], ...]  # `where` empty: column -> rows
    empty: tuple[Cell, ...]  # cells the edits write that are empty


def reads_of(
    text: str, proposal: ProposedEdits, catalog: Catalog, sheet_hash: str
) -> Reads:
    if _whole(text, proposal, catalog):
        return Reads(sheet_hash, '', (), (), ())
    return _seen(text, proposal, catalog)


def holds(reads: Reads, text: str, proposal: ProposedEdits, catalog: Catalog) -> bool:
    """On another sheet: a `whole` answer never does."""
    return reads.whole is None and reads == _seen(text, proposal, catalog)


def why_not(
    reads: Reads | None, text: str, proposal: ProposedEdits, catalog: Catalog
) -> str | None:
    """Why the answer does not hold on this sheet, for the user; None
    when nothing was saved to tell."""
    if reads is None:
        return None
    if reads.whole is not None:
        return 'its answer holds only on the sheet it was read on'
    now = _seen(text, proposal, catalog)
    if now.structure != reads.structure:
        return 'the columns or the sample labels changed'
    said = [
        *_moved(dict(reads.held), dict(now.held), catalog),
        *_filled(_empty(reads), _empty(now), catalog),
    ]
    return '; '.join(said[:2]) or None


def to_json(reads: Reads) -> dict:
    return {
        'whole': reads.whole,
        'structure': reads.structure,
        'held': {name: [list(c) for c in cells] for name, cells in reads.held},
        'blank': {column: list(rows) for column, rows in reads.blank},
        'empty': [list(c) for c in reads.empty],
    }


def from_json(data) -> Reads | None:
    """None for anything malformed: the answer then holds on its own
    sheet only."""
    try:
        whole, structure = data['whole'], data['structure']
        if not (whole is None or isinstance(whole, str)) or not isinstance(
            structure, str
        ):
            return None
        held = tuple(
            (name, tuple(_cell(c) for c in cells))
            for name, cells in sorted(data['held'].items())
        )
        blank = tuple(
            (column, tuple(_row(r) for r in rows))
            for column, rows in sorted(data['blank'].items())
        )
        empty = tuple(_cell(c) for c in data['empty'])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if not all(isinstance(n, str) for n, _ in held) or not all(
        isinstance(c, str) for c, _ in blank
    ):
        return None
    return Reads(whole, structure, held, blank, empty)


def _cell(c) -> Cell:
    column, row = c
    if not isinstance(column, str):
        raise TypeError
    return column, _row(row)


def _row(r) -> int:
    if not isinstance(r, int) or isinstance(r, bool):
        raise TypeError
    return r


def _whole(text: str, proposal: ProposedEdits, catalog: Catalog) -> bool:
    if proposal.unresolved or not coverage(text, proposal, catalog).complete:
        return True
    for edit in proposal.edits:
        target = catalog.column(edit.target)
        anchor = catalog.column(edit.anchor.column) if edit.anchor else None
        if target is None or (edit.anchor and anchor is None):
            return True
        if edit.anchor and not in_words(edit.anchor.equals, edit.said):
            return True
        problems, _, doubts = value_problems(edit, target, anchor, catalog)
        if problems or doubts:
            return True
    return False


def _seen(text: str, proposal: ProposedEdits, catalog: Catalog) -> Reads:
    names = _names(text, proposal, catalog)
    held = tuple(
        (
            name,
            tuple(
                (column.id, row)
                for column in catalog.columns
                for group in column.groups
                if same_value(name, group.value)
                for row in sorted(group.rows)
            ),
        )
        for name in sorted(names)
    )
    blank = {}
    empty = []
    for edit in proposal.edits:
        if isinstance(edit.rows, Where) and edit.rows.equals is None:
            column = catalog.column(edit.rows.where)
            if column is not None:
                blank[column.id] = tuple(
                    sorted(r for g in column.groups if g.value is None for r in g.rows)
                )
        target = catalog.column(edit.target)
        try:
            rows, _ = resolve_rows(edit.rows, catalog)
        except Refusal:
            rows = ()
        if target is not None:
            held_by = {r: g.value for g in target.groups for r in g.rows}
            empty += [(target.id, r) for r in rows if same_value('', held_by.get(r))]
    return Reads(
        None,
        _structure(catalog),
        held,
        tuple(sorted(blank.items())),
        tuple(sorted(set(empty))),
    )


def _names(text: str, proposal: ProposedEdits, catalog: Catalog) -> set[str]:
    patterns = sheet_values(catalog)
    names = {t for t, p in patterns.items() if p.search(text)}
    news = {n.value for e in proposal.edits if (n := value(e.new)) is not None}
    for edit in proposal.edits:
        names |= {edit.anchor.equals} if edit.anchor else set()
        if isinstance(edit.rows, Where) and edit.rows.equals is not None:
            names.add(edit.rows.equals)
        if edit.said_old and edit.said_old.strip():
            names.add(edit.said_old.strip())
        target = catalog.column(edit.target)
        if target is None:
            continue
        anchor = catalog.column(edit.anchor.column) if edit.anchor else None
        # a number inside a text value ('20min') is that value's
        inside = [m.span() for p in patterns.values() for m in p.finditer(edit.said)]
        names |= {
            format(n.value.normalize(), 'f')
            for n in value_numbers(edit.said, target, anchor, catalog)
            if n.value not in news
            and not any(s <= n.start and n.end <= e for s, e in inside)
        }
    return {n for n in names if n.strip()}


def _structure(catalog: Catalog) -> str:
    return sha256(
        json.dumps(
            [
                [
                    [c.id, c.column.section.label, c.column.header, c.formula]
                    + [list(c.inputs) if c.inputs is not None else None]
                    for c in catalog.columns
                ],
                [[s.row, s.label, s.subbatch] for s in catalog.samples],
            ]
        ).encode()
    )


def _empty(reads: Reads) -> set[Cell]:
    """The cells that must be empty: written, or picked by `where` empty."""
    return set(reads.empty) | {(c, r) for c, rows in reads.blank for r in rows}


def _moved(
    before: dict[str, tuple[Cell, ...]],
    now: dict[str, tuple[Cell, ...]],
    catalog: Catalog,
) -> list[str]:
    """ "Solvent 2 name (1: Preparation) no longer holds 'DMF' on samples 1–3"."""
    said = []
    for name in sorted(set(before) | set(now)):
        old, new = set(before.get(name, ())), set(now.get(name, ()))
        said += [
            f'{c} no longer holds {name!r} on samples {r}'
            for c, r in _by_column(old - new, catalog)
        ]
        said += [
            f'{c} now holds {name!r} on samples {r}'
            for c, r in _by_column(new - old, catalog)
        ]
    return said


def _filled(before: set[Cell], now: set[Cell], catalog: Catalog) -> list[str]:
    return [
        *(
            f'{c} on samples {r} is no longer empty'
            for c, r in _by_column(before - now, catalog)
        ),
        *(
            f'{c} on samples {r} is now empty'
            for c, r in _by_column(now - before, catalog)
        ),
    ]


def _by_column(cells: set[Cell], catalog: Catalog) -> list[tuple[str, str]]:
    """(column name, sample names) per column of the cells."""
    rows: dict[str, list[int]] = {}
    for column, row in sorted(cells):
        rows.setdefault(column, []).append(row)
    known = {s.row for s in catalog.samples}
    out = []
    for column_id, rs in rows.items():
        column = catalog.column(column_id)
        name = column_name(column) if column is not None else column_id
        out.append((name, catalog.names([r for r in rs if r in known]) or '?'))
    return out
