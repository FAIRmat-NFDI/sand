"""Read the layout of a hysprint-style sheet from the file itself.

There is no fixed template: row 1 names the step groups, row 2 the
columns, rows 3+ are samples. Groups are found the way NOMAD's batch
parser finds them (pandas `header=[0, 1]`): a row-1 label opens a group
that runs until the next label, so merged cells are not required.
"""

import io
import re
from dataclasses import dataclass

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

GROUP_ROW = 1
HEADER_ROW = 2
FIRST_SAMPLE_ROW = 3
EXPERIMENT_INFO = 'Experiment Info'
SAMPLE_HEADER = 'Sample'
SUBBATCH_HEADER = 'Subbatch'
NOMAD_ID_HEADER = 'Nomad ID'

_POSITION_RE = re.compile(r'^(\d+)\s*:\s*(.+)$')


@dataclass(frozen=True)
class Section:
    label: str  # as written in row 1, e.g. '2: Spin Coating'
    step_type: str  # 'Spin Coating'; 'Experiment Info' for the info group
    position: int | None  # the 'N:' prefix; None for Experiment Info
    first_col: int  # 1-based, inclusive
    last_col: int


@dataclass(frozen=True)
class Column:
    index: int  # 1-based: column A is 1
    header: str  # the column name in row 2, e.g. 'Rotation speed [rpm]'
    section: Section


@dataclass(frozen=True)
class SampleRow:
    row: int  # 1-based sheet row
    sample: str | None  # this row's value in the Sample column, e.g. '7'


@dataclass(frozen=True)
class SheetStructure:
    sheet_name: str
    sections: tuple[Section, ...]
    columns: tuple[Column, ...]
    samples: tuple[SampleRow, ...]


def _text(value) -> str:
    return '' if value is None else str(value).strip()


def _sample(value) -> str | None:
    """Sample names as users say them: 1.0 and 1 are both '1'."""
    if value is None or _text(value) == '':
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return _text(value)


def _section(label: str, first_col: int, last_col: int) -> Section:
    m = _POSITION_RE.match(label)
    if m:
        return Section(label, m.group(2).strip(), int(m.group(1)), first_col, last_col)
    return Section(label, label, None, first_col, last_col)


def _info_column(columns: tuple[Column, ...], header: str) -> int | None:
    return next(
        (
            col.index
            for col in columns
            if col.section.step_type == EXPERIMENT_INFO and col.header == header
        ),
        None,
    )


def _first_sheet(xlsx: bytes) -> Worksheet:
    return load_workbook(io.BytesIO(xlsx)).worksheets[0]


def read_structure(xlsx: bytes) -> SheetStructure:
    """Sections, columns and sample rows of the first sheet (the one the
    batch parser reads)."""
    return _structure(_first_sheet(xlsx))


def _structure(ws: Worksheet) -> SheetStructure:
    width = max(
        (c for c in range(1, ws.max_column + 1) if _text(ws.cell(HEADER_ROW, c).value)),
        default=0,
    )

    starts = [c for c in range(1, width + 1) if _text(ws.cell(GROUP_ROW, c).value)]
    sections = tuple(
        _section(_text(ws.cell(GROUP_ROW, a).value), a, b - 1)
        for a, b in zip(starts, [*starts[1:], width + 1])
    )

    columns = tuple(
        Column(c, _text(ws.cell(HEADER_ROW, c).value), sec)
        for sec in sections
        for c in range(sec.first_col, sec.last_col + 1)
        if _text(ws.cell(HEADER_ROW, c).value)
    )

    sample_col = _info_column(columns, SAMPLE_HEADER)
    samples = tuple(
        SampleRow(
            r, _sample(ws.cell(r, sample_col).value) if sample_col is not None else None
        )
        for r in range(FIRST_SAMPLE_ROW, ws.max_row + 1)
        if any(_text(ws.cell(r, c).value) for c in range(1, width + 1))
    )
    return SheetStructure(ws.title, sections, columns, samples)


# --- ingest checks ----------------------------------------------------------


def ingest_problems(xlsx: bytes) -> list[str]:
    """Why this sheet cannot be edited safely; empty means it can.

    Every check guards addressing: a cell must have exactly one
    (step, column, sample) address, or an edit could land in the wrong
    place.
    """
    try:
        ws = _first_sheet(xlsx)
    except Exception as exc:  # any openpyxl/zip failure means "not an xlsx"
        return [f'not a readable .xlsx file ({exc})']
    s = _structure(ws)
    if not s.columns:
        return ['row 2 has no column headers']

    problems = [
        *_group_problems(ws, s),
        *_column_problems(ws, s),
        *_sample_problems(ws, s),
    ]
    problems += [
        f'merged cells {rng.coord} below row 1: cells must be single to be edited'
        for rng in ws.merged_cells.ranges
        if rng.max_row > GROUP_ROW
    ]
    return problems


def _group_problems(ws: Worksheet, s: SheetStructure) -> list[str]:
    problems = []
    first = s.sections[0] if s.sections else None
    if first is None or first.first_col != 1 or first.label != EXPERIMENT_INFO:
        problems.append(
            f"cell A1 must be '{EXPERIMENT_INFO}' "
            f'(found {_text(ws.cell(GROUP_ROW, 1).value)!r})'
        )
    seen: dict[int, str] = {}
    for sec in s.sections[1:]:
        where = get_column_letter(sec.first_col)
        if sec.position is None:
            problems.append(
                f"step label {sec.label!r} (cell {where}1) has no 'N:' position"
            )
        elif sec.position in seen:
            problems.append(
                f'step position {sec.position} is used twice '
                f'(cells {seen[sec.position]}1 and {where}1)'
            )
        else:
            seen[sec.position] = where
    return problems


def _column_problems(ws: Worksheet, s: SheetStructure) -> list[str]:
    problems = []
    headed = {col.index for col in s.columns}
    for c in range(1, ws.max_column + 1):
        if c not in headed and any(
            _text(ws.cell(r, c).value) for r in range(FIRST_SAMPLE_ROW, ws.max_row + 1)
        ):
            problems.append(
                f'column {get_column_letter(c)} has values but no header in row 2'
            )
    for sec in s.sections:
        by_header: dict[str, list[str]] = {}
        for col in s.columns:
            if col.section == sec:
                by_header.setdefault(col.header, []).append(
                    get_column_letter(col.index)
                )
        problems += [
            f'{header!r} appears twice in {sec.label!r} (columns {", ".join(cols)})'
            for header, cols in by_header.items()
            if len(cols) > 1
        ]
    return problems


def _sample_problems(ws: Worksheet, s: SheetStructure) -> list[str]:
    problems = [
        f"'{EXPERIMENT_INFO}' has no {header!r} column"
        for header in (NOMAD_ID_HEADER, SAMPLE_HEADER)
        if _info_column(s.columns, header) is None
    ]
    if not s.samples:
        return [*problems, 'no sample rows below the headers']

    rows = [r.row for r in s.samples]
    problems += [
        f'row {r} is empty between sample rows'
        for r in range(rows[0], rows[-1] + 1)
        if r not in set(rows)
    ]
    if _info_column(s.columns, SAMPLE_HEADER) is None:
        return problems

    # Samples may restart at 1 in each subbatch: the address is then
    # (subbatch, sample), and a bare "sample 1" is resolved later.
    subbatch_col = _info_column(s.columns, SUBBATCH_HEADER)
    seen: dict[tuple, int] = {}
    for r in s.samples:
        if r.sample is None:
            problems.append(f'row {r.row} has no {SAMPLE_HEADER!r} value')
            continue
        subbatch = _sample(ws.cell(r.row, subbatch_col).value) if subbatch_col else None
        key = (subbatch, r.sample)
        if key in seen:
            name = f'sample {r.sample}' + (
                f' of subbatch {subbatch}' if subbatch is not None else ''
            )
            problems.append(f'{name} appears twice (rows {seen[key]} and {r.row})')
        else:
            seen[key] = r.row
    return problems
