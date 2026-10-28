"""The audit trail of a sheet: one entry per cell a confirmed edit
wrote, or a formula recomputed because of one.

Every entry says what changed, which input and words it came from, and
who confirmed it. The log is the history, not a diff: a cell edited by
two inputs has two entries. `check` replays the log on the original
file and verifies the result is the final file, cell for cell, so the
three of them (original, final, log) hold together.

For now the log is only shown in the UI; storing it in NOMAD beside the
sheet is issue #120.
"""

import datetime
import hashlib
import json
from dataclasses import asdict, dataclass

from openpyxl.utils import get_column_letter

from sand.sheet_edit.apply import Step
from sand.sheet_edit.catalog import Catalog, build
from sand.sheet_edit.ground import Grounded
from sand.sheet_edit.render import value_text
from sand.sheet_edit.workbook import cell_changes
from sand.sheet_edit.write import Write, write_cells

# cell changes a log entry stands for; a 'type' change always comes with
# a 'value' change of the same cell and is implied by the new value
_LOGGED_FIELDS = {'value', 'computed value'}


@dataclass(frozen=True)
class Confirmation:
    by: str  # the user, as the workflow names them
    at: datetime.datetime


@dataclass(frozen=True)
class Entry:
    input: int  # 1-based, in the order the inputs were given
    cell: str  # 'M3'
    row: int
    column: int  # 1-based, like Column.index
    section: str
    header: str
    sample: str  # as the user says it: '1', '1 of subbatch 0', or 'row 5'
    old: object
    new: object
    # None on a formula result: nobody said it, nobody confirmed it, it
    # followed from a confirmed edit of the same input
    said: str | None
    confirmed_by: str | None
    confirmed_at: str | None  # ISO 8601

    @property
    def by_formula(self) -> bool:
        return self.said is None


@dataclass(frozen=True)
class Changelog:
    sheet: str  # the edited sheet's name
    original: str  # sha256 of the uploaded file
    final: str  # sha256 of the file the last input left
    model: str  # model and prompt version that proposed the edits
    entries: tuple[Entry, ...]  # in the order the cells were written


class CheckFailed(Exception):
    """The final file is not the original plus the log."""


def changelog(
    original: bytes,
    steps: list[Step],
    confirmations: dict[str, Confirmation],
    model: str,
) -> Changelog:
    """`steps` are what `apply_all` returned for `original`; `confirmations`
    hold who confirmed which edit, by the confirmation key `apply_all`
    was given. Only written cells make entries: an input nobody confirmed
    contributes nothing."""
    entries: list[Entry] = []
    for number, step in enumerate(steps, 1):
        catalog = step.interpretation.catalog
        edited = _confirmed_cells(step, confirmations)
        for change in step.written:
            if change.field not in _LOGGED_FIELDS:
                continue
            cell = (change.row, change.column)
            if change.field == 'value' and cell not in edited:
                raise ValueError(
                    f'{_coord(*cell)} was written without a confirmed edit'
                )
            edit, confirmed = edited.get(cell, (None, None))
            column = catalog.columns[change.column - 1]
            assert column.column.index == change.column
            entries.append(
                Entry(
                    input=number,
                    cell=_coord(*cell),
                    row=change.row,
                    column=change.column,
                    section=column.column.section.label,
                    header=column.column.header,
                    sample=catalog.names((change.row,)),
                    old=change.before,
                    new=change.after,
                    said=edit.edit.said if edit else None,
                    confirmed_by=confirmed.by if confirmed else None,
                    confirmed_at=(
                        confirmed.at.isoformat(timespec='seconds')
                        if confirmed
                        else None
                    ),
                )
            )
    catalog = steps[0].interpretation.catalog if steps else build(original)
    return Changelog(
        sheet=catalog.structure.sheet_name,
        original=_hash(original),
        final=_hash(steps[-1].state) if steps else _hash(original),
        model=model,
        entries=tuple(entries),
    )


def _confirmed_cells(
    step: Step, confirmations: dict[str, Confirmation]
) -> dict[tuple[int, int], tuple[Grounded, Confirmation]]:
    """(row, column) -> the confirmed edit that wrote it, and by whom."""
    catalog = step.interpretation.catalog
    cells = {}
    for key, edit in step.interpretation.ready.items():
        if key not in confirmations:
            continue
        column = _column_index(edit.edit.target, catalog)
        for row in edit.rows:
            cells[(row, column)] = (edit, confirmations[key])
    return cells


def check(original: bytes, final: bytes, log: Changelog) -> None:
    """Replay the log on `original` and raise `CheckFailed` unless the
    result is `final`: same hashes, every logged change made as logged,
    no other cell different."""
    if _hash(original) != log.original:
        raise CheckFailed('the original file is not the one the log was made for')
    if _hash(final) != log.final:
        raise CheckFailed('the final file is not the one the log was made for')
    state = original
    for number in sorted({e.input for e in log.entries}):
        logged = [e for e in log.entries if e.input == number]
        writes = [Write(e.row, e.column, e.new) for e in logged if not e.by_formula]
        result = write_cells(state, writes)
        made = {
            (c.row, c.column): (value_text(c.before), value_text(c.after))
            for c in result.changes
            if c.field in _LOGGED_FIELDS
        }
        # values are compared as text: the log may have been through JSON
        wanted = {
            (e.row, e.column): (value_text(e.old), value_text(e.new)) for e in logged
        }
        if made != wanted:
            raise CheckFailed(
                f'input {number}: the log says {_describe(wanted)}, '
                f'replaying it changes {_describe(made)}'
            )
        state = result.xlsx
    unlogged = cell_changes(state, final)
    if unlogged:
        raise CheckFailed(
            'the final file differs from the original plus the log at '
            + ', '.join(sorted({_coord(c.row, c.column) for c in unlogged}))
        )


# --- JSON and text ---------------------------------------------------------


def to_json(log: Changelog) -> str:
    """With a `digest` of everything else, so a stored log can be checked
    for tampering on its own."""
    body = asdict(log)
    body['entries'] = [_json_entry(e) for e in log.entries]
    return json.dumps({**body, 'digest': _digest(body)}, indent=1, ensure_ascii=False)


def from_json(text: str) -> Changelog:
    data = json.loads(text)
    digest = data.pop('digest')
    if digest != _digest(data):
        raise CheckFailed('the log was changed after it was written')
    entries = tuple(Entry(**e) for e in data.pop('entries'))
    return Changelog(entries=entries, **data)


def render(log: Changelog) -> str:
    """The log as the user reads it: one block per edit, one line per
    cell:

        1. "solvent 2 to DMSO for samples 1 to 3" · confirmed by Yaru, 2026-10-28 10:16
           M3  1: Cleaning UV-Ozone · Solvent 2  sample 1  IPA → DMSO
        1. by formula
           F3  Experiment Info · Nomad ID  sample 1  KIT_…_B1_0_1 → KIT_…_B2_0_1
    """
    lines = [
        f'sheet     {log.sheet}',
        f'original  sha256 {log.original}',
        f'final     sha256 {log.final}',
        f'model     {log.model}',
    ]
    if not log.entries:
        return '\n'.join([*lines, '', 'no cell changed'])
    width = {
        'cell': max(len(e.cell) for e in log.entries),
        'where': max(len(_where(e)) for e in log.entries),
        'sample': max(len(_sample_text(e)) for e in log.entries),
    }
    block = None
    for e in log.entries:
        head = (e.input, e.said, e.confirmed_by, e.confirmed_at)
        if head != block:
            block = head
            lines.extend(['', _block_line(e)])
        lines.append(
            f'   {e.cell:<{width["cell"]}}  {_where(e):<{width["where"]}}  '
            f'{_sample_text(e):<{width["sample"]}}  '
            f'{value_text(e.old)} → {value_text(e.new)}'
        )
    return '\n'.join(lines)


def _block_line(e: Entry) -> str:
    if e.by_formula:
        return f'{e.input}. by formula'
    at = datetime.datetime.fromisoformat(e.confirmed_at or '')
    when = at.strftime('%Y-%m-%d %H:%M')
    return f'{e.input}. "{e.said}" · confirmed by {e.confirmed_by}, {when}'


def _where(e: Entry) -> str:
    return f'{e.section} · {e.header}'


def _sample_text(e: Entry) -> str:
    return e.sample if e.sample.startswith('row ') else f'sample {e.sample}'


# --- helpers ---------------------------------------------------------------


def _column_index(column_id: str, catalog: Catalog) -> int:
    column = catalog.column(column_id)
    assert column is not None  # ground refused the edit otherwise
    return column.column.index


def _coord(row: int, column: int) -> str:
    return f'{get_column_letter(column)}{row}'


def _describe(changes: dict[tuple[int, int], tuple[str, str]]) -> str:
    if not changes:
        return 'nothing'
    return ', '.join(
        f'{_coord(*cell)} {old or "(empty)"} → {new or "(empty)"}'
        for cell, (old, new) in sorted(changes.items())
    )


def _json_entry(e: Entry) -> dict:
    """Cell values that JSON cannot hold (dates, durations) are stored as
    their text; `check` compares text, so the replay still works."""
    data = asdict(e)
    for field in ('old', 'new'):
        if not isinstance(data[field], str | int | float | bool | None):
            data[field] = value_text(data[field])
    return data


def _digest(body: dict) -> str:
    return _hash(json.dumps(body, sort_keys=True, ensure_ascii=False).encode())


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
