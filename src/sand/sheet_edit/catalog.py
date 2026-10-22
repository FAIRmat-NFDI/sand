"""One entry per column: the only vocabulary the LLM may use.

Everything is read from the file: no column, unit or type is assumed.
"""

import datetime
import re
from dataclasses import dataclass
from typing import Literal

from openpyxl.worksheet.worksheet import Worksheet

from sand.sheet_edit.formulas import Unsupported, references
from sand.sheet_edit.workbook import (
    Column,
    SheetStructure,
    first_sheet,
    sheet_structure,
)
from sand.sheet_headers import INDEXED_GROUPS, STRIP_RE, classify, norm_key, step_key

ValueType = Literal[
    'empty', 'int', 'float', 'text', 'bool', 'date', 'yyyymmdd', 'mixed'
]

_UNIT_RE = re.compile(r'\[([^\]]+)\]')
_NUMBER_RE = re.compile(r'(?<![\w.])\d+(?![\w.])')
_YYYYMMDD_DIGITS = 8


@dataclass(frozen=True)
class Family:
    """Columns that describe one thing. 'Solvent 1 name' and 'Solvent 1
    volume' are (solvents, 1); a step's other numbered columns share only
    the number (Seq-Evaporation 'Material name 1', 'Thickness 1')."""

    group: str | None  # 'solvents', 'rotation_steps', ...; None: number only
    index: int


@dataclass(frozen=True)
class ColumnInfo:
    id: str  # 'C033': from the column's place in the sheet, which edits never change
    column: Column
    family: Family | None  # 'Solvent 1 volume [uL]' -> Family('solvents', 1)
    unit: str | None  # from '[...]' in the header
    type: ValueType  # of the values in the sample rows
    formula: bool  # a sample cell holds a formula: not writable
    inputs: tuple[str, ...] | None  # ids the formulas read; None: unknown


@dataclass(frozen=True)
class Catalog:
    structure: SheetStructure
    columns: tuple[ColumnInfo, ...]


def column_id(index: int) -> str:
    return f'C{index:03d}'


def build(xlsx: bytes) -> Catalog:
    ws = first_sheet(xlsx)
    results = first_sheet(xlsx, data_only=True)
    structure = sheet_structure(ws)
    rows = [s.row for s in structure.samples]
    headed = {col.index for col in structure.columns}
    columns = tuple(
        ColumnInfo(
            id=column_id(col.index),
            column=col,
            family=_family(col),
            unit=_unit(col.header),
            type=value_type(
                col.header, [results.cell(r, col.index).value for r in rows]
            ),
            formula=any(ws.cell(r, col.index).data_type == 'f' for r in rows),
            inputs=_inputs(ws, col.index, rows, headed),
        )
        for col in structure.columns
    )
    return Catalog(structure, columns)


def _family(col: Column) -> Family | None:
    spec = classify(col.header, INDEXED_GROUPS.get(step_key(col.section.step_type)))
    if spec['kind'] == 'list':
        return Family(spec['list'], spec['idx'])
    numbers = _NUMBER_RE.findall(STRIP_RE.sub('', col.header))
    return Family(None, int(numbers[0])) if len(numbers) == 1 else None


def _unit(header: str) -> str | None:
    m = _UNIT_RE.search(header)
    return m.group(1).strip() if m else None


def value_type(header: str, values: list) -> ValueType:
    kinds = {_kind(v) for v in values} - {'empty'}
    if kinds == {'int'} and _is_date_column(header, values):
        return 'yyyymmdd'
    if kinds == {'int', 'float'}:
        return 'float'
    if len(kinds) > 1:
        return 'mixed'
    return kinds.pop() if kinds else 'empty'


def _kind(v) -> ValueType:
    if v is None or (isinstance(v, str) and not v.strip()):
        return 'empty'
    if isinstance(v, bool):
        return 'bool'
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer()):
        return 'int'
    if isinstance(v, float):
        return 'float'
    if isinstance(v, datetime.date | datetime.time | datetime.timedelta):
        return 'date'
    return 'text'


def _is_date_column(header: str, values: list) -> bool:
    """KIT writes dates as the number 20260914. Both must hold: the header
    says date, and every value is a real calendar day."""
    if 'date' not in norm_key(header).split('_'):
        return False
    return all(_is_day(str(int(v))) for v in values if _kind(v) == 'int')


def _is_day(digits: str) -> bool:
    if len(digits) != _YYYYMMDD_DIGITS:
        return False
    try:
        datetime.datetime.strptime(digits, '%Y%m%d')
    except ValueError:
        return False
    return True


def _inputs(
    ws: Worksheet, column: int, rows: list[int], headed: set[int]
) -> tuple[str, ...] | None:
    read: set[int] = set()
    for r in rows:
        cell = ws.cell(r, column)
        if cell.data_type != 'f':
            continue
        if not isinstance(cell.value, str):  # array or data-table formula
            return None
        try:
            read |= {ref.column for ref in references(cell.value)}
        except Unsupported:
            return None
    if not read <= headed:  # reads a cell outside the catalogued columns
        return None
    return tuple(column_id(c) for c in sorted(read))
