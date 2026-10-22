"""The hysprint sheet's header vocabulary, shared by extraction
(sand.hysprint) and sheet editing (sand.sheet_edit).

Copied from nomad-entry-data (src/hzb_pool/to_json.py) - keep in sync.
"""

import re


def step_key(step_type: str) -> str:
    """'Slot Die Coating' -> 'slot_die_coating'."""
    return step_type.lower().replace(' ', '_').replace('-', '_')


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


def norm_key(text: str) -> str:
    k = re.sub(r'_+', '_', re.sub(r'[^a-z0-9]+', '_', text.strip().lower())).strip('_')
    return CORRECTIONS.get(k, k)


def plural(key: str) -> str:
    return key if key.endswith('s') else key + 's'


def classify(header: str, indexed_group: tuple | None) -> dict:
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
