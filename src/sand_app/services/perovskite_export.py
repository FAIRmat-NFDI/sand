"""Split extracted perovskite cells into NOMAD-ready archive entries.

Ported from lamalab-org/perla-extract (src/perla_extract/export.py). The LLM
returns one ``PerovskiteSolarCells`` object holding a ``cells`` list; NOMAD wants
one ``LLMExtractedPerovskiteSolarCell`` entry per cell, with renamed keys,
flattened {value, unit} objects, and a derived ``layer_order``. Two quality
filters run first: a PCE self-consistency check and a hallucination guard against
the source text.

Public entrypoint: ``convert_cells_to_nomad_entries``.
"""

import copy
import math
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any


M_DEF = 'perovskite_solar_cell_database.llm_extraction_schema.LLMExtractedPerovskiteSolarCell'

# Mappings: "model key" -> "NOMAD key"
KEY_MAPPING = {
    'bandgap': 'band_gap',
    'PCE_at_the_start_of_the_experiment': 'PCE_at_start',
    'PCE_at_the_end_of_experiment': 'PCE_at_end',
    'a_ions': 'ions_a_site',
    'b_ions': 'ions_b_site',
    'x_ions': 'ions_x_site',
}

# Context key -> target unit for value/unit flattening.
UNIT_CONVERSIONS = {
    'stability': 'hour',
    'PCE_T80': 'hour',
}

# Keys whose {value, unit} object must be kept as a nested structure.
PRESERVE_STRUCTURE = ['additional_parameters']

# Keys whose {value, unit} object is split into ``<key>`` and ``<key>_unit``.
SPLIT_VALUE_UNIT = ['concentration']

# Keys dropped during conversion (mirrors perla-extract).
SKIP_KEYS = ['additives']


# --------------------------------------------------------------------------- #
# Transform: model shape -> NOMAD shape
# --------------------------------------------------------------------------- #
def get_layer_order(layers: Any) -> str | None:
    """Return non-null layer names as a pipe-delimited string.

    The perovskite-solar-cell-database normalizer (v1.2+) splits ``layer_order``
    on ``'|'`` and matches each name against ``layers[].name``; a comma-joined
    string (as in older perla-extract) is parsed as a single name and triggers a
    "names in layer_order does not match available layers" warning, which also
    prevents the device-structure figure from being generated.
    """
    if not layers or not isinstance(layers, list):
        return None
    names = [layer['name'] for layer in layers if layer.get('name')]
    return ' | '.join(names) if names else None


def convert_with_pint(value_dict: dict, parent_key: str | None, ureg=None) -> Any:
    """Flatten a {value, unit} dict to a number, converting units where defined.

    ``pint`` ships transitively with nomad-lab; if it is unavailable or a
    conversion fails, the raw value is returned unchanged.
    """
    val = value_dict.get('value')
    unit = value_dict.get('unit')

    if val is None:
        return None
    if not unit or unit == '%':
        return val

    try:
        if ureg is None:
            from pint import UnitRegistry

            ureg = UnitRegistry()
        quantity = ureg.Quantity(val, unit)
        if parent_key in UNIT_CONVERSIONS:
            target = UNIT_CONVERSIONS[parent_key]
            if quantity.check(target):
                return float(quantity.to(target).magnitude)
        return float(quantity.magnitude)
    except Exception:
        return val


def traverse_and_transform(obj: Any, parent_key: str | None = None, ureg=None) -> Any:
    """Recursively clean nulls, flatten value/unit objects, and rename keys."""
    if isinstance(obj, list):
        processed = [traverse_and_transform(item, parent_key, ureg) for item in obj]
        return [item for item in processed if item is not None]

    if isinstance(obj, dict):
        if 'value' in obj and parent_key in SPLIT_VALUE_UNIT:
            return {
                parent_key: obj.get('value'),
                f'{parent_key}_unit': obj.get('unit'),
            }

        if 'value' in obj and parent_key not in PRESERVE_STRUCTURE:
            return convert_with_pint(obj, parent_key, ureg)

        new_dict: dict[str, Any] = {}

        if obj.get('layers'):
            order = get_layer_order(obj['layers'])
            if order:
                new_dict['layer_order'] = order

        for key, value in obj.items():
            if key in SKIP_KEYS or value is None:
                continue

            new_key = KEY_MAPPING.get(key, key)
            transformed = traverse_and_transform(value, key, ureg)

            if transformed is None:
                continue
            if isinstance(transformed, (dict, list)) and not transformed:
                continue
            if isinstance(transformed, dict) and key in SPLIT_VALUE_UNIT:
                new_dict.update(transformed)
            else:
                new_dict[new_key] = transformed

        if not new_dict and obj:
            return None
        return new_dict

    return obj


# --------------------------------------------------------------------------- #
# Filter 1: PCE self-consistency (drops tandems/modules and inconsistent cells)
# --------------------------------------------------------------------------- #
def remove_pce_check(data: dict) -> dict:
    """Keep cells whose PCE is consistent with Jsc*Voc*FF, or incomplete.

    Operates on the pre-transform shape where metrics are {value, unit} dicts.
    Cells with Voc >= 1.56 V (tandems/modules) or PCE >= 27.5 % are dropped.
    """
    new_data: dict[str, list] = {'cells': []}
    for i, cell in enumerate(data['cells'] or []):
        if (
            ((cell.get('pce') or {'value': 28}).get('value') or 28) < 27.5
            and ((cell.get('voc') or {'value': 1}).get('value') or 1) < 1.56
        ):
            if (
                (cell.get('pce', {'value': 0}) or {'value': 0}).get('value', 0) == 0
                or (cell.get('jsc', {'value': 0}) or {'value': 0}).get('value', 0) == 0
                or (cell.get('voc', {'value': 0}) or {'value': 0}).get('value', 0) == 0
                or (cell.get('ff', {'value': 0}) or {'value': 0}).get('value', 0) == 0
            ):
                new_data['cells'].append(data['cells'][i])
                continue
            if math.isclose(
                ((cell.get('pce') or {'value': 99}).get('value') or 99),
                (
                    ((cell.get('jsc') or {'value': 0}).get('value') or 0)
                    * ((cell.get('voc') or {'value': 0}).get('value') or 0)
                    * ((cell.get('ff') or {'value': 0}).get('value') or 0)
                )
                / 100,
                abs_tol=0.2,
            ):
                new_data['cells'].append(data['cells'][i])
                continue
    return new_data


# --------------------------------------------------------------------------- #
# Filter 2: hallucination guard (numbers must appear in the source text)
# --------------------------------------------------------------------------- #
def normalize_float(val: float, max_decimals: int = 6) -> float:
    return float(
        Decimal(str(val)).quantize(
            Decimal(f'1.{"0" * max_decimals}'), rounding=ROUND_HALF_UP
        )
    )


def number_matches_text(val: float, text: str) -> str | None:
    val = normalize_float(val)
    canonical = format(val, 'f').rstrip('0').rstrip('.')

    if re.search(rf'\b{re.escape(canonical)}0*\b', text):
        return f'strict decimal match: {canonical}'
    if re.search(rf'\b{re.escape(str(val))}\b', text):
        return f'exact match: {val}'
    fmt2 = f'{val:.2f}'
    if re.search(rf'\b{re.escape(fmt2)}\b', text):
        return f'2-decimal match: {fmt2}'
    if val >= 1:
        shifted = val / 100
        if 0 < shifted < 1:
            shifted_str = format(shifted, 'f').rstrip('0').rstrip('.')
            if '.' in shifted_str and len(shifted_str.split('.')[-1]) <= 3:
                if re.search(rf'\b{re.escape(shifted_str)}0*\b', text):
                    return f'strict /100 match: {val}->{shifted_str}'
    if math.isclose(val, round(val)):
        int_val = str(int(round(val)))
        if re.search(rf'\b{int_val}\b', text):
            return f'integer match: {int_val}'
        if re.search(rf'\b{int_val}\s*%', text):
            return f'integer percent match: {int_val}%'
    if abs(val) < 1:
        scaled = val * 1000
        if math.isclose(scaled, round(scaled)):
            scaled_int = str(int(round(scaled)))
            if re.search(rf'\b{scaled_int}\b', text):
                return f'scaled x1000 match: {val}->{scaled_int}'
    return None


def remove_hallucinated_big_four_area(data: dict, source_text: str) -> dict:
    """Delete top-level numeric metrics (PCE/Voc/Jsc/FF/area) not found in text."""
    cells = copy.deepcopy(data['cells'])
    for i, cell in enumerate(cells):
        values = {
            key: prop.get('value')
            for key, prop in cell.items()
            if isinstance(prop, dict) and isinstance(prop.get('value'), (float, int))
        }
        if not values:
            continue
        for key, val in values.items():
            if val is None:
                continue
            if number_matches_text(val, source_text) is None:
                del cells[i][key]
    return {'cells': cells}


def filter_unwanted(data: dict, source_text: str | None) -> dict:
    data = remove_pce_check(data)
    if source_text:
        data = remove_hallucinated_big_four_area(data, source_text)
    return data


# --------------------------------------------------------------------------- #
# Postprocess: canonicalize units before flattening (perla postprocessing.py)
# --------------------------------------------------------------------------- #
_UREG = None
_DEFAULT_UNITS_BY_TYPE = None


def _unit_registry():
    """Lazily build the pint registry + default-unit-by-dimensionality map.

    Mirrors perla-extract's configuration.py. Returns ``(None, None)`` if pint
    is unavailable so normalization degrades to a no-op.
    """
    global _UREG, _DEFAULT_UNITS_BY_TYPE
    if _UREG is not None:
        return _UREG, _DEFAULT_UNITS_BY_TYPE
    try:
        from pint import UnitRegistry

        ureg = UnitRegistry()
        try:
            ureg.define('sun = 1 kW/m^2')
        except Exception:
            pass
        mapping = {
            ureg.percent.dimensionality: (ureg.percent, '%'),
            (ureg.ampere / ureg.centimeter**2).dimensionality: (
                ureg.milliampere / ureg.centimeter**2,
                'mA cm^-2',
            ),
            ureg.volt.dimensionality: (ureg.volt, 'V'),
            ureg.nanometer.dimensionality: (ureg.nanometer, 'nm'),
            (ureg.meter**2).dimensionality: (ureg.centimeter**2, 'cm^2'),
            ureg.day.dimensionality: (ureg.second, 's'),
            ureg.celsius.dimensionality: (ureg.celsius, '°C'),
            (ureg.mg / ureg.mL).dimensionality: (ureg.mg / ureg.mL, 'mg/mL'),
            (ureg.mW / ureg.cm**2).dimensionality: (ureg.mW / ureg.cm**2, 'mW cm^-2'),
            (ureg.mol / ureg.L).dimensionality: (ureg.mol / ureg.L, 'mol/L'),
            ureg.eV.dimensionality: (ureg.eV, 'eV'),
            (ureg.meter**3).dimensionality: (ureg.milliliter, 'mL'),
        }
        _UREG, _DEFAULT_UNITS_BY_TYPE = ureg, mapping
    except Exception:
        _UREG, _DEFAULT_UNITS_BY_TYPE = None, None
    return _UREG, _DEFAULT_UNITS_BY_TYPE


def remove_ml_concentrations(data: dict) -> dict:
    """Drop solute concentrations expressed in the invalid unit 'mL'."""
    data = copy.deepcopy(data)
    for cell in data.get('cells', []) or []:
        for layer in cell.get('layers', []) or []:
            for dep in layer.get('deposition', []) or []:
                solution = dep.get('solution') or {}
                solutes = solution.get('solutes')
                if not isinstance(solutes, list):
                    continue
                for solute in solutes:
                    conc = solute.get('concentration')
                    if isinstance(conc, dict) and conc.get('unit') == 'mL':
                        solute.pop('concentration', None)
    return data


def _normalize_walk(data: Any, ureg, default_by_type) -> Any:
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                data[key] = _normalize_walk(value, ureg, default_by_type)
            elif isinstance(value, list):
                data[key] = [
                    _normalize_walk(item, ureg, default_by_type)
                    if isinstance(item, (dict, list, tuple))
                    else item
                    for item in value
                ]
            elif (
                key == 'value'
                and 'unit' in data
                and data['value'] is not None
                and data['unit'] is not None
            ):
                try:
                    quantity = ureg.Quantity(value, ureg.Unit(data['unit']))
                    dim = quantity.dimensionality
                    if dim in default_by_type:
                        default_unit, default_str = default_by_type[dim]
                        data['value'] = quantity.to(default_unit).magnitude
                        data['unit'] = default_str
                except Exception:
                    pass
        return data
    if isinstance(data, list):
        return [
            _normalize_walk(item, ureg, default_by_type)
            if isinstance(item, (dict, list, tuple))
            else item
            for item in data
        ]
    return data


def normalize_units(data: dict) -> dict:
    """Convert every {value, unit} pair to the schema's default unit by type."""
    ureg, default_by_type = _unit_registry()
    if ureg is None:
        return data
    return _normalize_walk(data, ureg, default_by_type)


def postprocess(data: dict) -> dict:
    """perla-extract postprocessing: drop mL concentrations, canonicalize units."""
    data = remove_ml_concentrations(data)
    data = normalize_units(data)
    return data


# --------------------------------------------------------------------------- #
# Split + wrap into NOMAD archive entries
# --------------------------------------------------------------------------- #
def cell_to_archive(cell: dict[str, Any], doi: str | None = None, ureg=None) -> dict[str, Any]:
    """Transform one cell and wrap it in a NOMAD archive envelope."""
    transformed = traverse_and_transform(cell, ureg=ureg) or {}
    data: dict[str, Any] = {'m_def': M_DEF}
    if doi:
        data['DOI_number'] = f'https://www.doi.org/{doi}'
    data.update(transformed)
    return {'data': data}


def convert_cells_to_nomad_entries(
    cells: list[dict[str, Any]],
    source_text: str | None = None,
    doi: str | None = None,
    ureg=None,
) -> list[dict[str, Any]]:
    """Filter, transform, and split extracted cells into NOMAD archive entries.

    Mirrors perla-extract's flow: ``postprocess`` (canonicalize units) ->
    ``filter_unwanted`` (PCE-consistency + hallucination guard) -> transform and
    split into one archive dict per surviving cell, ready to upload to NOMAD.
    """
    data = postprocess({'cells': cells or []})
    data = filter_unwanted(data, source_text)
    return [cell_to_archive(cell, doi=doi, ureg=ureg) for cell in data['cells']]


def cell_display_name(data: dict[str, Any]) -> str:
    """Human-readable name for a cell archive's data dict."""
    composition = data.get('perovskite_composition') or {}
    formula = composition.get('formula')
    architecture = data.get('device_architecture')
    if formula and architecture:
        return f'{formula} ({architecture})'
    return formula or architecture or 'Perovskite solar cell'
