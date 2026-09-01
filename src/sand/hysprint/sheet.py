"""Lay a hysprint {samples, steps} archive out as the sheet NOMAD ingests.

Copied from nomad-entry-data (src/hzb_pool/to_csv.py + the shared header
vocabulary from to_json.py) - keep in sync. The column layout comes from
the template sand/schemas/all_steps.csv (the HZB master sheet's two header
rows); NOMAD's hysprint batch parser matches any .xlsx in an upload.
"""

import csv
import io
import math
import re
from functools import lru_cache
from importlib import resources

# the HZB sheets are semicolon-delimited (comma = decimal)
DELIM = ';'

# Fixed name of the derived sheet in the experiment upload: regenerating
# overwrites it in place, so the derived entry id (and link) never changes.
DERIVED_SHEET_MAINFILE = 'hysprint_experiment.xlsx'

# Pristine extraction result + provenance, stored next to the sheet. NOT
# named *.archive.json, so NOMAD never parses it as an entry.
EXTRACTED_JSON_MAINFILE = 'hysprint_experiment.extracted.json'


def step_key(step_type: str) -> str:
    """'Slot Die Coating' -> 'slot_die_coating'."""
    return step_type.lower().replace(' ', '_').replace('-', '_')


# --- the header-classification vocabulary (from to_json.py) ---------------

STRIP_RE = re.compile(r'\[.+?\]|\(.+?\)')  # drop '[unit]' / '(parenthetical)'
NUMBERED_RE = re.compile(r'^(?P<base>.+?)\s+(?P<n>\d+)(?:\s+(?P<attr>.+))?$')

# Step types where several DIFFERENT columns share an index and zip into ONE
# list of objects ('Rotation speed 1' + 'Rotation time 1' -> rotation_steps[0]).
INDEXED_GROUPS = {
    'cleaning_uv_ozone': (
        'cleaning_steps',
        {'solvent': 'solvent', 'time': 'time', 'temperature': 'temperature'},
    ),
    'cleaning_o2_plasma': (
        'cleaning_steps',
        {'solvent': 'solvent', 'time': 'time', 'temperature': 'temperature'},
    ),
    'spin_coating': (
        'rotation_steps',
        {
            'rotation_speed': 'speed',
            'rotation_time': 'time',
            'acceleration': 'acceleration',
        },
    ),
}

BARE_LIST = {'solute': ('solutes', 'name'), 'solvent': ('solvents', 'name')}

ATMOSPHERE_FIELDS = {
    'rel_humidity': 'relative_humidity',
    'room_gb_humidity': 'relative_humidity',
    'room_rel_humidity': 'relative_humidity',
    'room_temperature': 'temperature',
    'gb_start_oxygen_level': 'start_oxygen_level_ppm',
    'start_gb_oxygen_level': 'start_oxygen_level_ppm',
    'gb_oxygen_level': 'start_oxygen_level_ppm',
    'gb_end_oxygen_level': 'end_oxygen_level_ppm',
    'end_gb_oxygen_level': 'end_oxygen_level_ppm',
    'gb_start_temperature': 'start_gb_temperature',
    'start_gb_temperature': 'start_gb_temperature',
    'gb_end_temperature': 'end_gb_temperature',
    'end_gb_temperature': 'end_gb_temperature',
    'gb_start_water_level': 'start_water_level_ppm',
    'start_gb_water_level': 'start_water_level_ppm',
    'gb_end_water_level': 'end_water_level_ppm',
    'end_gb_water_level': 'end_water_level_ppm',
}

CORRECTIONS = {
    'annealing_athmosphere': 'annealing_atmosphere',
    'anti_solvent_dropping_heigt': 'anti_solvent_dropping_height',
    'droplet_density': 'droplet_density_x',
    'nomad_id': 'lab_id',  # NOMAD stores the 'Nomad ID' column as lab_id
}

STEP_CORRECTIONS = {
    'evaporation': {'rate': 'rate_target'},
}


def norm_key(text: str) -> str:
    k = re.sub(r'_+', '_', re.sub(r'[^a-z0-9]+', '_', text.strip().lower())).strip('_')
    return CORRECTIONS.get(k, k)


def plural(key: str) -> str:
    return key if key.endswith('s') else key + 's'


# --- the sheet template (from to_csv.py) ----------------------------------

# Step types the master sheet omits but the schema/parser support.
EXTRA_TEMPLATES = {
    'Annealing': [
        'Datetime',
        'Operator',
        'Annealing time [min]',
        'Annealing temperature [°C]',
        'Annealing atmosphere',
        'Relative humidity [%]',
        'Notes',
    ],
}

# The parser appends map_atmosphere to EVERY process step, so an atmosphere
# block is a per-step OPTION - emitted only when a step carries it.
ATMOSPHERE_COLUMNS = [
    ('relative_humidity', 'rel. humidity [%]'),
    ('temperature', 'Room temperature [°C]'),
    ('start_oxygen_level_ppm', 'Start GB Oxygen level [ppm]'),
    ('end_oxygen_level_ppm', 'End GB Oxygen level [ppm]'),
    ('start_water_level_ppm', 'Start GB Water level [ppm]'),
    ('end_water_level_ppm', 'End GB Water level [ppm]'),
    ('start_gb_temperature', 'Start GB Temperature [°C]'),
    ('end_gb_temperature', 'End GB Temperature [°C]'),
]
_ATMOS_FIELDS = {f for f, _ in ATMOSPHERE_COLUMNS}


@lru_cache(maxsize=1)
def _template() -> tuple[list[str], dict[str, list[str]], dict[str, list[str]]]:
    """(experiment-info headers, {step_type: headers}, {spin_variant: headers}).
    Reads the template's two header rows; the rest of the file is the
    human-facing data-entry guide."""
    text = (resources.files('sand') / 'schemas/all_steps.csv').read_text('utf-8-sig')
    rows = list(csv.reader(io.StringIO(text), delimiter=DELIM))
    row0, row1 = rows[0], rows[1]
    width = max(len(row0), len(row1))
    row0 += [''] * (width - len(row0))
    row1 += [''] * (width - len(row1))
    starts = [i for i, v in enumerate(row0) if v.strip()]
    spans = [
        (starts[k], starts[k + 1] if k + 1 < len(starts) else width)
        for k in range(len(starts))
    ]

    ei: list[str] = []
    steps: dict[str, list[str]] = {}
    spins: dict[str, list[str]] = {}
    for a, b in spans:
        step_type = re.sub(r'^\d+:\s*', '', row0[a]).strip()
        headers = [row1[c] for c in range(a, b) if row1[c].strip()]
        if step_type == 'Experiment Info':
            ei = headers
        elif step_type == 'Spin Coating':  # 3 variants, told apart by a marker
            joined = ' '.join(headers).lower()
            kind = (
                'antisolvent'
                if 'anti solvent' in joined
                else 'vacuum'
                if 'vacuum quenching' in joined
                else 'gas'
                if 'gas quenching' in joined
                else 'antisolvent'
            )
            spins[kind] = headers
        else:
            steps[step_type] = headers
    steps.update({k: v for k, v in EXTRA_TEMPLATES.items() if k not in steps})
    return ei, steps, spins


# --- header expansion / value lookup (from to_csv.py, verbatim logic) -----


def _classify(header: str, indexed_group: tuple | None) -> dict:
    name = STRIP_RE.sub('', header).strip()
    m = NUMBERED_RE.match(name)
    if m and m.group('attr'):  # '{base} {n} {attr}'
        return {
            'kind': 'list',
            'list': plural(norm_key(m.group('base'))),
            'field': norm_key(m.group('attr')),
            'idx': int(m.group('n')),
        }
    if m and indexed_group and norm_key(m.group('base')) in indexed_group[1]:
        return {
            'kind': 'list',
            'list': indexed_group[0],
            'field': indexed_group[1][norm_key(m.group('base'))],
            'idx': int(m.group('n')),
        }
    if indexed_group and norm_key(name) in indexed_group[1]:  # un-indexed
        return {
            'kind': 'list',
            'list': indexed_group[0],
            'field': indexed_group[1][norm_key(name)],
            'idx': 1,
            'unindexed': True,
        }
    if m and norm_key(m.group('base')) in BARE_LIST:  # bare 'Solute N'
        lname, field = BARE_LIST[norm_key(m.group('base'))]
        return {'kind': 'list', 'list': lname, 'field': field, 'idx': int(m.group('n'))}
    if norm_key(name) in ATMOSPHERE_FIELDS:
        return {'kind': 'atmos', 'field': ATMOSPHERE_FIELDS[norm_key(name)]}
    return {'kind': 'flat', 'field': norm_key(name)}


def _renumber(header: str, new_idx: int) -> str:
    """'Solvent 1 volume [uL]' -> 'Solvent 2 volume [uL]' (first standalone 1)."""
    return re.sub(r'(?<!\d)1(?!\d)', str(new_idx), header, count=1)


def _spin_variant(variants: list[dict]) -> list[str]:
    """Which of the three Spin Coating templates fits, by the fields present."""
    spins = _template()[2]
    keys = set().union(*(set(v) for v in variants))
    if any(k.startswith('anti_solvent') for k in keys):
        return spins.get('antisolvent', [])
    if any(k.startswith('vacuum_quenching') for k in keys):
        return spins.get('vacuum', [])
    if any(k.startswith('gas_quenching') for k in keys) or 'gas' in keys:
        return spins.get('gas', [])
    return spins.get('antisolvent', [])  # plain spin -> the fullest template


def _section_columns(
    step_key: str, headers: list[str], variants: list[dict], warn
) -> list[tuple[str, dict]]:
    """Ordered (header, spec) for a section, numbered list columns grown to
    the longest list any variant carries."""
    indexed_group = INDEXED_GROUPS.get(step_key)
    raw = [(h, _classify(h, indexed_group)) for h in headers]

    fields: dict[str, list[tuple[str, str, bool]]] = {}
    for h, sp in raw:
        if sp['kind'] == 'list' and (sp.get('idx', 1) == 1 or sp.get('unindexed')):
            seen = fields.setdefault(sp['list'], [])
            if not any(f == sp['field'] for f, _, _ in seen):
                seen.append((sp['field'], h, sp.get('unindexed', False)))
    needed = {L: max([len(v.get(L) or []) for v in variants] + [1]) for L in fields}

    out: list[tuple[str, dict]] = []
    done: set[str] = set()
    for h, sp in raw:
        if sp['kind'] != 'list':
            out.append((h, sp))
            continue
        lst = sp['list']
        if lst in done:
            continue
        done.add(lst)
        cols, n = fields[lst], needed[lst]
        if cols and cols[0][2] and n > 1:  # bare single-step col can't be numbered
            warn(
                f"{step_key}: '{lst}' needs {n} entries but its column is "
                'not indexed; kept the first'
            )
            n = 1
        for idx in range(1, n + 1):
            for field, idx1_header, bare in cols:
                header = (
                    idx1_header if (bare or idx == 1) else _renumber(idx1_header, idx)
                )
                out.append(
                    (header, {'kind': 'list', 'list': lst, 'field': field, 'idx': idx})
                )

    present = (
        set().union(*(set(v.get('atmosphere') or {}) for v in variants))
        if variants
        else set()
    )
    for field, header in ATMOSPHERE_COLUMNS:
        if field in present:
            out.append((header, {'kind': 'atmos', 'field': field}))
    for field in sorted(present - _ATMOS_FIELDS):
        warn(f'{step_key}: atmosphere.{field} has no canonical column, value dropped')
    return out


def _value(spec: dict, obj: dict, step_key: str | None):
    if spec['kind'] == 'flat':
        field = spec['field']
        field = STEP_CORRECTIONS.get(step_key or '', {}).get(field, field)
        return obj.get(field)
    if spec['kind'] == 'atmos':
        return (obj.get('atmosphere') or {}).get(spec['field'])
    lst = obj.get(spec['list']) or []
    i = spec['idx'] - 1
    return lst[i].get(spec['field']) if 0 <= i < len(lst) else None


def _column_has_data(spec: dict, variants: list[dict], step_key: str | None) -> bool:
    """0 / False count as data; only null / "" / [] / {} are empty."""
    return any(_value(spec, v, step_key) not in (None, '', [], {}) for v in variants)


def _fmt(v) -> str:
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'TRUE' if v else 'FALSE'
    return str(v)


def _present(obj: dict) -> set[str]:
    """Every value-bearing path, in the vocabulary the columns consume - a
    set-difference against _consumed finds anything the sheet cannot hold."""
    paths: set[str] = set()
    for k, v in obj.items():
        # position has no column: the sheet encodes it as section order
        if k in ('step_type', 'samples', 'position_in_experimental_plan') or v in (
            None,
            '',
            [],
            {},
        ):
            continue
        if k == 'atmosphere' and isinstance(v, dict):
            paths |= {f'atmosphere.{sk}' for sk in v}
        elif isinstance(v, list):
            paths |= {f'{k}.{f}' for it in v if isinstance(it, dict) for f in it}
        else:
            paths.add(k)
    return paths


def _consumed(cols: list[tuple[str, dict]], step_key: str | None) -> set[str]:
    out: set[str] = set()
    for _, sp in cols:
        if sp['kind'] == 'flat':
            out.add(
                STEP_CORRECTIONS.get(step_key or '', {}).get(sp['field'], sp['field'])
            )
        elif sp['kind'] == 'atmos':
            out.add(f'atmosphere.{sp["field"]}')
        else:
            out.add(f'{sp["list"]}.{sp["field"]}')
    return out


def _sample_set(step: dict, all_ids: list[str]) -> set[str]:
    """A step's samples membership as an explicit id set ("all" expanded)."""
    return (
        set(all_ids) if step.get('samples') == 'all' else set(step.get('samples') or [])
    )


def _step_sections(steps: list[dict], all_ids: list[str]) -> list[dict]:
    """Regroup deduplicated steps into positional sections. Positions are
    trusted only when EVERY step carries one; else the adjacency heuristic
    (same type + disjoint samples continues the section)."""
    by_position = bool(steps) and all(
        'position_in_experimental_plan' in s for s in steps
    )
    if by_position:
        steps = sorted(steps, key=lambda s: s['position_in_experimental_plan'])
    sections: list[dict] = []
    for step in steps:
        sset = _sample_set(step, all_ids)
        pos = step.get('position_in_experimental_plan')
        cur = sections[-1] if sections else None
        same_slot = (
            cur is not None
            and cur['step_type'] == step['step_type']
            and (pos == cur['pos'] if by_position else cur['samples'].isdisjoint(sset))
        )
        if same_slot:
            cur['variants'].append(step)
            cur['samples'] |= sset
        else:
            sections.append(
                {
                    'step_type': step['step_type'],
                    'pos': pos,
                    'variants': [step],
                    'samples': set(sset),
                }
            )
    return sections


# kept branch-for-branch close to the nomad-entry-data origin
def experiment_to_grid(experiment: dict, warn) -> list[list[str]]:  # noqa: PLR0912
    """The {samples, steps} archive -> a rectangular grid (two header rows +
    one row per sample). `warn(msg)` collects anything not representable."""
    ei_headers, step_templates, _ = _template()
    samples = experiment.get('samples', [])
    steps = experiment.get('steps', [])
    all_ids = [str(s.get('lab_id') or f'row{i + 1}') for i, s in enumerate(samples)]

    positioned = sum(1 for s in steps if 'position_in_experimental_plan' in s)
    if 0 < positioned < len(steps):
        warn(
            f'only {positioned}/{len(steps)} steps carry '
            'position_in_experimental_plan - partial numbering ignored, '
            'sections rebuilt by the adjacency heuristic'
        )

    laid_out: list[dict] = [
        {
            'label': 'Experiment Info',
            'key': None,
            'variants': None,
            'cols': [(h, _classify(h, None)) for h in ei_headers],
        }
    ]
    n = 0
    for sec in _step_sections(steps, all_ids):
        if sec['step_type'] == 'Spin Coating':
            headers = _spin_variant(sec['variants'])
        else:
            headers = step_templates.get(sec['step_type'])
        if not headers:
            warn(
                f"no template for step type '{sec['step_type']}' - its "
                f'{len(sec["variants"])} step(s) omitted'
            )
            continue
        key = step_key(sec['step_type'])
        cols = _section_columns(key, headers, sec['variants'], warn)
        for v in sec['variants']:  # flag any field the sheet can't hold
            for path in sorted(_present(v) - _consumed(cols, key)):
                warn(f'{sec["step_type"]}.{path}: no column, value dropped')
        cols = [(h, sp) for h, sp in cols if _column_has_data(sp, sec['variants'], key)]
        if not cols:  # every cell blank -> drop the section
            continue
        n += 1
        laid_out.append(
            {
                'label': f'{n}: {sec["step_type"]}',
                'key': key,
                'variants': sec['variants'],
                'cols': cols,
            }
        )

    for s in samples:  # experiment-info coverage
        cols = laid_out[0]['cols']
        for path in sorted(_present({**s, 'step_type': None}) - _consumed(cols, None)):
            warn(f'experiment_info.{path}: no column, value dropped')

    row0, row1 = [], []
    for sec in laid_out:
        row0 += [sec['label']] + [''] * (len(sec['cols']) - 1)
        row1 += [h for h, _ in sec['cols']]

    data_rows = []
    for lab_id, s in zip(all_ids, samples):
        row = []
        for sec in laid_out:
            if sec['variants'] is None:  # Experiment Info
                row += [_fmt(_value(sp, s, None)) for _, sp in sec['cols']]
                continue
            variant = next(
                (v for v in sec['variants'] if lab_id in _sample_set(v, all_ids)), None
            )
            row += (
                ['' for _ in sec['cols']]
                if variant is None
                else [_fmt(_value(sp, variant, sec['key'])) for _, sp in sec['cols']]
            )
        data_rows.append(row)
    return [row0, row1, *data_rows]


def to_sheet(archive: dict) -> tuple[list[list[str]], list[str]]:
    """(grid, issues): empty issues means every field fits the sheet."""
    issues: list[str] = []
    grid = experiment_to_grid(archive, issues.append)
    return grid, issues


def _typed(val: str):
    """A cell string -> what the xlsx cell should hold: int / float / str;
    '' stays empty. '5.0' keeps its float-ness so json round-trips."""
    if val == '':
        return None
    try:
        f = float(val)
        if not math.isfinite(f):  # nan/inf spellings are text, not numbers
            return val
    except ValueError:
        return val
    return int(f) if f.is_integer() and not re.search(r'[.eE]', val) else f


def grid_to_xlsx_bytes(grid: list[list[str]]) -> bytes:
    """The grid as an in-memory .xlsx (what the hysprint batch parser matches)."""
    from openpyxl import Workbook

    wb = Workbook()
    for row in grid:
        wb.active.append([_typed(v) for v in row])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
