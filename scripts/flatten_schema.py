#!/usr/bin/env python3
"""Regenerate the flattened extraction schema from the NOMAD API export.

Usage: python scripts/flatten_schema.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / 'src'))

from sand.services.schema_convert import flatten_schema  # noqa: E402

RAW_PATH = ROOT / 'src' / 'HySprint_SlotDieCoating.json'
OUT_PATH = ROOT / 'src' / 'sand' / 'flatten_HySprint_SlotDieCoating.json'

# The API export only contains the base Quenching class, but HZB entries use
# the AirKnifeQuenching / GasQuenching subclasses — their fields are patched
# in here (unit notation matches the export's pint long names).
QUENCHING_EXTRA_PROPS = {
    'gas': {
        'type': 'string',
        'description': 'The gas used for gas quenching, e.g. nitrogen.',
    },
    'air_knife_angle': {
        'type': 'number',
        'description': 'The angle of the air knife.',
        'unit': 'degree',
    },
    'air_knife_distance_to_thin_film': {
        'type': 'number',
        'description': 'The distance of the air knife to the thin film.',
        'unit': 'micrometer',
    },
}


# The export's SolutionPreparation base class is likewise empty; HZB entries
# use a subclass with these quantities (types/units copied from the export's
# Solution class, which declares the same quantities).
PREPARATION_EXTRA_PROPS = {
    'method': {
        'type': 'string',
        'description': 'How the solution was prepared.',
        'enum': ['Shaker', 'Stirring', 'Ultrasoncic', 'Waiting'],
    },
    'solvent_ratio': {
        'type': 'string',
        'description': 'The ratio of the solvents, as stated (e.g. "1:6").',
    },
    'temperature': {
        'type': 'number',
        'description': 'The temperature during preparation.',
        'unit': 'degree_Celsius',
    },
    'time': {
        'type': 'number',
        'description': 'The duration of the preparation.',
        'unit': 'minute',
    },
    'speed': {
        'type': 'number',
        'description': 'The shaking/stirring speed during preparation.',
        'unit': 'hertz',
    },
}


# Entry-reference quantities: NOMAD stores these as proxy strings and its
# normalizers try to resolve them, so an LLM-invented value like "PbI2"
# crashes processing ("Could not resolve PbI2"). They stay in the schema but
# get their description annotated so the model knows not to put names there.
# 'solution' is only a reference when string-typed (the top-level solution
# list of subsections must stay unannotated).
REF_PROPS = {'samples', 'instruments', 'steps', 'batch', 'chemical', 'reference'}
REF_IF_STRING = {'solution'}

REF_NOTE = (
    'ENTRY REFERENCE: this field must hold a reference to another NOMAD '
    'entry, never a plain name or free text. Omit it unless the text '
    'explicitly provides such a reference.'
)


def _annotate_references(node: dict) -> None:
    props = node.get('properties')
    if not isinstance(props, dict):
        return
    for name, prop in props.items():
        if name in REF_PROPS or (
            name in REF_IF_STRING and prop.get('type') == 'string'
        ):
            existing = prop.get('description') or ''
            prop['description'] = f'{REF_NOTE} {existing}'.strip()
            continue
        _annotate_references(prop.get('items', prop))


def _patch_preparation(node: dict) -> None:
    """Add the preparation subclass fields to every Solution occurrence."""
    props = node.get('properties')
    if isinstance(props, dict):
        preparation = props.get('preparation')
        if isinstance(preparation, dict):
            preparation.setdefault('properties', {}).update(
                PREPARATION_EXTRA_PROPS
            )
        for value in props.values():
            _patch_preparation(value.get('items', value))


def main() -> None:
    with open(RAW_PATH) as f:
        raw = json.load(f)
    flat = flatten_schema(raw)
    flat['properties']['quenching']['properties'].update(QUENCHING_EXTRA_PROPS)
    _patch_preparation(flat)
    _annotate_references(flat)
    with open(OUT_PATH, 'w') as f:
        json.dump(flat, f, indent=1)
    print(f'wrote {OUT_PATH}')


if __name__ == '__main__':
    main()
