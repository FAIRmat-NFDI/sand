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

GROUP_ROW = 1
HEADER_ROW = 2
FIRST_SAMPLE_ROW = 3
EXPERIMENT_INFO = 'Experiment Info'
SAMPLE_HEADER = 'Sample'

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
    index: int  # 1-based
    letter: str
    header: str
    section: Section


@dataclass(frozen=True)
class SampleRow:
    row: int  # 1-based sheet row
    label: str | None  # the Sample column's value; None if absent


@dataclass(frozen=True)
class SheetStructure:
    sheet_name: str
    sections: tuple[Section, ...]
    columns: tuple[Column, ...]
    samples: tuple[SampleRow, ...]


def _text(value) -> str:
    return '' if value is None else str(value).strip()


def _label(value) -> str | None:
    """Sample labels as users say them: 1.0 and 1 are both '1'."""
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


def read_structure(xlsx: bytes) -> SheetStructure:
    """Sections, columns and sample rows of the first sheet (the one the
    batch parser reads)."""
    ws = load_workbook(io.BytesIO(xlsx)).worksheets[0]
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
        Column(c, get_column_letter(c), _text(ws.cell(HEADER_ROW, c).value), sec)
        for sec in sections
        for c in range(sec.first_col, sec.last_col + 1)
        if _text(ws.cell(HEADER_ROW, c).value)
    )

    sample_col = next(
        (
            col.index
            for col in columns
            if col.section.step_type == EXPERIMENT_INFO and col.header == SAMPLE_HEADER
        ),
        None,
    )
    samples = tuple(
        SampleRow(
            r, _label(ws.cell(r, sample_col).value) if sample_col is not None else None
        )
        for r in range(FIRST_SAMPLE_ROW, ws.max_row + 1)
        if any(_text(ws.cell(r, c).value) for c in range(1, width + 1))
    )
    return SheetStructure(ws.title, sections, columns, samples)
