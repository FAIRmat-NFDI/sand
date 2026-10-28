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
import io
import json
import zipfile
from dataclasses import asdict, dataclass, fields

from sand.sheet_edit.apply import Step
from sand.sheet_edit.catalog import Catalog, build, column_id
from sand.sheet_edit.ground import Grounded
from sand.sheet_edit.render import value_text
from sand.sheet_edit.workbook import cell_changes, coord, sha256
from sand.sheet_edit.write import VerifyFailed, Write, WriteRefused, write_cells

# cell changes a log entry stands for; a 'type' change always comes with
# a 'value' change of the same cell and is implied by the new value
_LOGGED_FIELDS = {'value', 'computed value'}


@dataclass(frozen=True)
class Confirmation:
    by: str  # the user, as the workflow names them
    at: datetime.datetime  # timezone-aware; an audit time must not be ambiguous

    def __post_init__(self):
        if self.at.tzinfo is None:
            raise ValueError('a confirmation time needs a timezone')


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
    # all three None on a formula result: nobody said it, nobody confirmed
    # it, it followed from a confirmed edit of the same input
    said: str | None
    confirmed_by: str | None
    confirmed_at: str | None  # ISO 8601, UTC

    def __post_init__(self):
        """Also runs on a loaded log: a stored entry is trusted no more
        than a built one."""
        _typed(self, 'input', int)
        _typed(self, 'row', int)
        _typed(self, 'column', int)
        for name in ('cell', 'section', 'header', 'sample'):
            _typed(self, name, str)
        for name in ('said', 'confirmed_by', 'confirmed_at'):
            _typed(self, name, str | None)
        if self.input < 1 or self.row < 1 or self.column < 1:
            raise ValueError(f'{self.cell}: input, row and column count from 1')
        if self.cell != coord(self.row, self.column):
            raise ValueError(f'{self.cell} is not row {self.row}, column {self.column}')
        unset = {
            self.said is None,
            self.confirmed_by is None,
            self.confirmed_at is None,
        }
        if len(unset) != 1:
            raise ValueError(f'{self.cell}: words and confirmation go together')
        if self.confirmed_at is not None:
            datetime.datetime.fromisoformat(self.confirmed_at)  # raises otherwise

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

    def __post_init__(self):
        for name in ('sheet', 'original', 'final', 'model'):
            _typed(self, name, str)
        if not all(isinstance(e, Entry) for e in self.entries):
            raise ValueError('entries must be Entry')


def _typed(obj, name: str, kind) -> None:
    """bool is an int in Python; here it is not."""
    value = getattr(obj, name)
    if isinstance(value, bool) or not isinstance(value, kind):
        raise ValueError(f'{name} must be {kind}, not {value!r}')


class CheckFailed(Exception):
    """The final file is not the original plus the log, or the log
    cannot be read."""


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
                raise ValueError(f'{coord(*cell)} was written without a confirmed edit')
            edit, confirmed = edited.get(cell, (None, None))
            column = catalog.column(column_id(change.column))
            if column is None:
                raise ValueError(f'{coord(*cell)} is not a catalog column')
            entries.append(
                Entry(
                    input=number,
                    cell=coord(*cell),
                    row=change.row,
                    column=change.column,
                    section=column.column.section.label,
                    header=column.column.header,
                    sample=catalog.names((change.row,)),
                    old=change.before,
                    new=change.after,
                    said=edit.edit.said if edit else None,
                    confirmed_by=confirmed.by if confirmed else None,
                    confirmed_at=_utc_text(confirmed.at) if confirmed else None,
                )
            )
    catalog = steps[0].interpretation.catalog if steps else build(original)
    return Changelog(
        sheet=catalog.structure.sheet_name,
        original=sha256(original),
        final=sha256(steps[-1].state) if steps else sha256(original),
        model=model,
        entries=tuple(entries),
    )


def _confirmed_cells(
    step: Step, confirmations: dict[str, Confirmation]
) -> dict[tuple[int, int], tuple[Grounded, Confirmation]]:
    """(row, column) -> the confirmed edit that wrote it, and by whom."""
    catalog: Catalog = step.interpretation.catalog
    cells = {}
    for key, edit in step.interpretation.ready.items():
        if key not in confirmations:
            continue
        column = catalog.index(edit.edit.target)
        for row in edit.rows:
            cells[(row, column)] = (edit, confirmations[key])
    return cells


def _utc_text(at: datetime.datetime) -> str:
    return at.astimezone(datetime.UTC).isoformat(timespec='seconds')


# --- check -----------------------------------------------------------------


def check(original: bytes, final: bytes, log: Changelog) -> None:
    """Replay the log on `original` and raise `CheckFailed` unless the
    result is `final`: same hashes, every logged change made as logged,
    and nothing else different, in no part of the file."""
    if sha256(original) != log.original:
        raise CheckFailed('the original file is not the one the log was made for')
    if sha256(final) != log.final:
        raise CheckFailed('the final file is not the one the log was made for')
    state = original
    for number in sorted({e.input for e in log.entries}):
        logged = [e for e in log.entries if e.input == number]
        if len({(e.row, e.column) for e in logged}) != len(logged):
            raise CheckFailed(f'input {number}: a cell is logged twice')
        writes = [Write(e.row, e.column, e.new) for e in logged if not e.by_formula]
        try:
            result = write_cells(state, writes)
        except (WriteRefused, VerifyFailed) as why:
            raise CheckFailed(
                f'input {number}: the log cannot be replayed: {why}'
            ) from why
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
    # every part's content must be equal, not only the cells: a hidden
    # row or a changed column width is a change the log does not cover.
    # The writer patches the original's XML deterministically, so the
    # replay reproduces a file sand wrote byte for byte, uncompressed.
    if _contents(state) != _contents(final):
        cells = sorted({coord(c.row, c.column) for c in cell_changes(state, final)})
        where = f'at {", ".join(cells)}' if cells else 'outside the cell values'
        raise CheckFailed(
            f'the final file differs from the original plus the log {where}'
        )


def _contents(xlsx: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(xlsx)) as z:
        return {name: z.read(name) for name in z.namelist()}


def _describe(changes: dict[tuple[int, int], tuple[str, str]]) -> str:
    if not changes:
        return 'nothing'
    return ', '.join(
        f'{coord(*cell)} {old or "(empty)"} → {new or "(empty)"}'
        for cell, (old, new) in sorted(changes.items())
    )


# --- JSON and text ---------------------------------------------------------


def to_json(log: Changelog) -> str:
    """With a `digest` of everything else. It catches corruption and
    careless edits, not tampering: anyone can recompute a plain hash.
    Tamper-proofing needs NOMAD-side signing (#120)."""
    body = asdict(log)
    body['entries'] = [_json_entry(e) for e in log.entries]
    return json.dumps({**body, 'digest': _digest(body)}, indent=1, ensure_ascii=False)


def from_json(text: str) -> Changelog:
    """Raises `CheckFailed` for anything that is not an intact changelog."""
    try:
        data = json.loads(text)
    except ValueError as why:
        raise CheckFailed(f'not a changelog: {why}') from why
    if not isinstance(data, dict) or 'digest' not in data:
        raise CheckFailed('not a changelog: no digest')
    digest = data.pop('digest')
    if digest != _digest(data):
        raise CheckFailed('the log is corrupt or was edited after it was written')
    try:
        entries = tuple(_entry(e) for e in data['entries'])
        head = {f.name: data[f.name] for f in fields(Changelog) if f.name != 'entries'}
        return Changelog(entries=entries, **head)
    except (KeyError, TypeError, ValueError) as why:
        raise CheckFailed(f'not a changelog: {why!r}') from why


def _entry(data: dict) -> Entry:
    return Entry(**{f.name: data[f.name] for f in fields(Entry)})


def _json_entry(e: Entry) -> dict:
    """Cell values that JSON cannot hold (dates, durations) are stored as
    their text; `check` compares text, so the replay still works."""
    data = asdict(e)
    for field in ('old', 'new'):
        if not isinstance(data[field], str | int | float | bool | None):
            data[field] = value_text(data[field])
    return data


def _digest(body: dict) -> str:
    return sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode())


def render(log: Changelog) -> str:
    """The log as the user reads it: one block per edit, one line per
    cell:

        1. "solvent 2 to DMSO for samples 1 to 3" · confirmed by Yaru, 2026-10-28 10:16 UTC
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
    assert e.confirmed_at is not None  # Entry's invariant
    when = datetime.datetime.fromisoformat(e.confirmed_at).strftime('%Y-%m-%d %H:%M %Z')
    return f'{e.input}. "{e.said}" · confirmed by {e.confirmed_by}, {when}'


def _where(e: Entry) -> str:
    return f'{e.section} · {e.header}'


def _sample_text(e: Entry) -> str:
    return e.sample if e.sample.startswith('row ') else f'sample {e.sample}'
