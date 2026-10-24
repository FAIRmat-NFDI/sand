"""The catalog as the one markdown table the LLM reads.

Values are quoted because lab values hold commas, brackets and '+'
('Cs0.05(MA0.17FA0.83)0.95Pb(I0.83Br0.17)3', 'MAI+MABr'). The table is
only read, never parsed back: code resolves ids and samples from the
catalog itself.
"""

import datetime
import json

from sand.sheet_edit.catalog import Catalog, ColumnInfo
from sand.sheet_edit.formulas import as_text
from sand.sheet_edit.workbook import EXPERIMENT_INFO, SAMPLE_HEADER

EMPTY = '(empty)'


def table(catalog: Catalog) -> str:
    names = {c.id: c.column.header for c in catalog.columns}
    lines = [
        '| ID | Step | Parameter | Current value |',
        '|---|---|---|---|',
        *(
            '| '
            + ' | '.join(
                [
                    c.id,
                    _cell(c.column.section.label),
                    _cell(c.column.header),
                    _current(c, catalog, names),
                ]
            )
            + ' |'
            for c in catalog.columns
        ),
        '',
        f'Samples: {_sample_line(catalog)}',
    ]
    return '\n'.join(lines) + '\n'


def _current(c: ColumnInfo, catalog: Catalog, names: dict[str, str]) -> str:
    """The 'Current value' cell, e.g. '"3000"', '(empty)',
    '"120" samples 1–21; "115" samples 23–43' or
    'formula, read-only (from C007 Variation)'. Formulas come first so
    their results never look like editable values."""
    if c.formula:
        if not c.inputs:
            return 'formula, read-only'
        reads = ', '.join(f'{i} {_cell(names[i])}' for i in c.inputs)
        return f'formula, read-only (from {reads})'
    if (c.column.section.step_type, c.column.header) == (
        EXPERIMENT_INFO,
        SAMPLE_HEADER,
    ):
        return 'the sample labels, see Samples below'
    if not c.groups:  # no sample rows
        return EMPTY
    if len(c.groups) == 1:  # the same on every sample
        return _value(c.groups[0].value)
    return '; '.join(
        f'{_value(g.value)} samples {catalog.names(g.rows)}' for g in c.groups
    )


def _value(v) -> str:
    if v is None:
        return EMPTY
    if isinstance(v, datetime.date | datetime.time):
        text = v.isoformat()
    elif isinstance(v, datetime.timedelta):
        text = str(v)
    else:
        text = as_text(v)
    return _cell(json.dumps(text, ensure_ascii=False))


def _cell(text: str) -> str:
    """A '|' would end the table cell; a newline would end the row."""
    return text.replace('|', '\\|').replace('\n', '\\n')


def _sample_line(catalog: Catalog) -> str:
    if not catalog.samples:
        return 'none'
    rows = [s.row for s in catalog.samples]
    return f'{catalog.names(rows)} ({len(rows)} samples)'
