"""The sheet as the page shows it: what holds for every sample, the
steps with their filled parameters, what varies, what looks off, and
what the confirmed edits changed.

Everything is read from the catalog; nothing is assumed about the
columns beyond the Experiment Info identity columns. Values are the
texts the written table shows (`value_text`), and samples are grouped
by that text: 3000 and 3000.0 are one value, '8 times' and '8 times '
are one value too.
"""

from collections import Counter

from sand.sheet_edit.catalog import (
    Catalog,
    ColumnInfo,
    column_id,
    numbered,
    sample_ranges,
)
from sand.sheet_edit.changelog import Changelog, Entry
from sand.sheet_edit.preview import samples_text
from sand.sheet_edit.render import value_text
from sand.sheet_edit.workbook import (
    EXPERIMENT_INFO,
    NOMAD_ID_HEADER,
    SAMPLE_HEADER,
    SUBBATCH_HEADER,
    Section,
)
from sand.sheet_headers import norm_key

# who a row is, never a parameter and never a variation
_IDENTITY = {SAMPLE_HEADER, SUBBATCH_HEADER, NOMAD_ID_HEADER}
# a step's headline, when the sheet has these columns
_MATERIAL, _LAYER = 'material_name', 'layer_type'


def sheet_view(catalog: Catalog, log: Changelog | None = None) -> dict:
    """
    {'sheet': 'Experiment Data',
     'info': {'samples': 42, 'subbatches': ['0'],
              'rows': [{'row', 'label', 'subbatch'}],
              'fields': [{'column_id', 'header', 'name', 'unit', 'type', 'value'}]},
     'steps': [{'position', 'step_type', 'label', 'material', 'layer', 'empty',
                'varies', 'params': [{'column_id', 'header', 'name', 'unit',
                'type', 'family', 'formula', 'values': [{'value', 'rows',
                'samples'}]}]}],
     'variations': [{'column_id', 'step', 'header', 'name', 'unit', 'type',
                     'values': [...]}],
     'warnings': ['Sample 22 is missing', 'Step 7 (Spin Coating) is empty'],
     'changed': [{'column_id', 'step', 'header', 'row', 'sample', 'old', 'new',
                  'by_formula'}]}

    A field is an Experiment Info column with one value on every sample;
    a param is a step column with a value on some sample; a variation is
    any column (identity columns aside) with more than one value, an
    empty cell counting as one. `changed` is per cell, the value before
    the first confirmed edit against the one the last left; a cell edited
    back to its old value is not listed.
    """
    info: list[dict] = []
    steps: list[dict] = []
    variations: list[dict] = []
    warnings: list[str] = []
    for section in catalog.structure.sections:
        columns = [c for c in catalog.columns if c.column.section == section]
        values = {c.id: _values(c, catalog) for c in columns}
        if section.step_type == EXPERIMENT_INFO:
            info += [
                {**_column(c), 'value': values[c.id][0]['value']}
                for c in columns
                if c.column.header not in _IDENTITY
                and len(values[c.id]) == 1
                and values[c.id][0]['value'] is not None
            ]
        else:
            step = _step(section, columns, values)
            steps.append(step)
            if step['empty']:
                warnings.append(_empty_step(section))
        variations += [
            {**_column(c), 'step': section.label, 'values': values[c.id]}
            for c in columns
            if c.column.header not in _IDENTITY and len(values[c.id]) > 1
        ]
        # grouping by text hides a number typed as text; the type says so
        warnings += [
            f'{c.column.header} in {section.label} mixes numbers and text'
            for c in columns
            if c.type == 'mixed'
        ]
    return {
        'sheet': catalog.structure.sheet_name,
        'info': {
            'samples': len(catalog.samples),
            'subbatches': sorted({s.subbatch for s in catalog.samples} - {None}),
            # the rows the values name, with their labels: for sample chips
            'rows': [
                {'row': s.row, 'label': s.label, 'subbatch': s.subbatch}
                for s in catalog.samples
            ],
            'fields': info,
        },
        'steps': steps,
        'variations': variations,
        'warnings': [*_sample_warnings(catalog), *warnings],
        'changed': _changed(log, catalog) if log else [],
    }


def _column(c: ColumnInfo) -> dict:
    return {
        'column_id': c.id,
        'header': c.column.header,
        'name': c.name,
        'unit': c.unit,
        'type': c.type,
    }


def _values(c: ColumnInfo, catalog: Catalog) -> list[dict]:
    """The column's values as texts, each with the samples holding it,
    in order of first appearance; None for the empty cells."""
    merged: dict[str | None, list[int]] = {}
    for g in c.groups:
        text = None if g.value is None else value_text(g.value).strip()
        merged.setdefault(text, []).extend(g.rows)
    return [
        {
            'value': text,
            'rows': sorted(rows),
            'samples': samples_text(tuple(sorted(rows)), catalog),
        }
        for text, rows in merged.items()
    ]


def _step(section: Section, columns: list[ColumnInfo], values: dict) -> dict:
    params = [
        {
            **_column(c),
            'family': (
                {'group': c.family.group, 'index': c.family.index} if c.family else None
            ),
            'formula': c.formula,
            'values': values[c.id],
        }
        for c in columns
        if any(v['value'] is not None for v in values[c.id])
    ]
    return {
        'position': section.position,
        'step_type': section.step_type,
        'label': section.label,
        'material': _one_value(params, _MATERIAL),
        'layer': _one_value(params, _LAYER),
        'empty': not params,
        'varies': any(len(p['values']) > 1 for p in params),
        'params': params,
    }


def _one_value(params: list[dict], key: str) -> str | None:
    for p in params:
        if norm_key(p['header']) == key and len(p['values']) == 1:
            return p['values'][0]['value']
    return None


def _empty_step(section: Section) -> str:
    return f'Step {section.position} ({section.step_type}) is empty'


def _sample_warnings(catalog: Catalog) -> list[str]:
    """Gaps in the sample numbers, labels used twice, rows without one.
    The subbatch is named only when the sheet needs it to tell samples
    apart, as the catalog names samples."""

    def where(subbatch: str | None) -> str:
        if catalog.needs_subbatch and subbatch is not None:
            return f' of subbatch {subbatch}'
        return ''

    warnings = [
        f'Row {s.row} has no {SAMPLE_HEADER} value'
        for s in catalog.samples
        if s.label is None
    ]
    numbers: dict[tuple[str | None, str], list[int]] = {}
    for s in catalog.samples:
        parts = numbered(s.label) if s.label is not None else None
        if parts is not None:
            numbers.setdefault((s.subbatch, parts[0]), []).append(parts[1])
    for (subbatch, prefix), found in numbers.items():
        present = set(found)
        missing = [
            f'{prefix}{n}'
            for n in range(min(found), max(found) + 1)
            if n not in present
        ]
        if len(missing) == 1:
            warnings.append(f'Sample {missing[0]}{where(subbatch)} is missing')
        elif missing:
            warnings.append(
                f'Samples {sample_ranges(missing)}{where(subbatch)} are missing'
            )
    counts = Counter((s.subbatch, s.label) for s in catalog.samples if s.label)
    for (subbatch, label), n in counts.items():
        if n > 1:
            times = {2: 'twice'}.get(n, f'{n} times')
            warnings.append(f'Sample {label}{where(subbatch)} appears {times}')
    return warnings


def _changed(log: Changelog, catalog: Catalog) -> list[dict]:
    first: dict[tuple[int, int], Entry] = {}
    last: dict[tuple[int, int], Entry] = {}
    for e in log.entries:
        cell = (e.column, e.row)
        first.setdefault(cell, e)
        last[cell] = e
    known = {s.row for s in catalog.samples}
    changed = []
    for cell in sorted(first):
        old = value_text(first[cell].old).strip()
        new = value_text(last[cell].new).strip()
        if old == new:
            continue
        e = last[cell]
        changed.append(
            {
                'column_id': column_id(e.column),
                'step': e.section,
                'header': e.header,
                'row': e.row,
                'sample': catalog.names((e.row,)) if e.row in known else e.sample,
                'old': old,
                'new': new,
                # a formula cell is only ever recomputed, never written by words
                'by_formula': e.by_formula,
            }
        )
    return changed
