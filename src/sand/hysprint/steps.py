import json
import re
from functools import lru_cache
from importlib import resources

SELECT_SYSTEM = (
    "You classify ONE fabrication step from a researcher's description of it. "
    'Output ONLY JSON of the form {"step_type": "<one of the step types the '
    'schema allows>"}.'
)

FILL_SYSTEM = """You transcribe ONE fabrication step done to a batch of perovskite solar-cell samples \
into JSON.

Output ONLY a JSON object matching the provided schema. One `variants` entry per GROUP of samples that \
got the SAME parameters at this step; each entry has "samples" (a list of the BARE sample labels — just \
the identifiers like "1" or "4", NOT phrases like "sample 1" — or "all" for every sample) plus the \
parameters that were stated.

- Fill ONLY parameters the text states; omit anything not said — do NOT guess or invent.
- Use each value exactly as stated (the number as said); do not convert units, scale, or round.
- For a group given as a difference ("same as sample 1 but ..."), fill its FULL parameters: take the \
referenced group's values (they are in this same description) and apply the change.
- Use only field names that appear in the provided schema."""


@lru_cache(maxsize=1)
def full_schema() -> dict:
    """The full HZB batch-experiment schema (the canonical artifact)."""
    schema_file = resources.files('sand') / 'schemas/hzb_experiment.schema.json'
    return json.loads(schema_file.read_text())


@lru_cache(maxsize=1)
def step_types() -> tuple[str, ...]:
    return tuple(
        d['properties']['step_type']['const']
        for d in full_schema()['$defs'].values()
        if 'step_type' in d['properties']
    )


def select_schema() -> dict:
    """The SELECT extraction target: exactly one of the known step types.
    The enum makes the pipeline's validation reject off-list answers."""
    return {
        'type': 'object',
        'additionalProperties': False,
        'required': ['step_type'],
        'properties': {'step_type': {'enum': list(step_types())}},
    }


def fill_schema(step_type: str) -> dict:
    """The FILL extraction target for one step type:
    {step_type: <const>, variants: [{samples, ...the step's fields}]}."""
    return _strip_units(to_single_step_schema(full_schema(), step_type))


def _step_key(step_type: str) -> str:
    """'Slot Die Coating' -> 'slot_die_coating' — the `$defs` key for a step type."""
    return step_type.lower().replace(' ', '_').replace('-', '_')


def to_single_step_schema(full: dict, step_type: str) -> dict:
    """Derive the extraction target for ONE step: `step_type` becomes a top-level
    const; bookkeeping fields (plan position, datetime, operator) are dropped —
    they are addressing/identity, never step content. `samples` stays in label
    form. An unknown step type raises ValueError."""
    step_def = full['$defs'].get(_step_key(step_type))
    if step_def is None or 'step_type' not in step_def['properties']:
        raise ValueError(
            f'unknown step type {step_type!r}; expected one of {sorted(step_types())}'
        )
    step = json.loads(json.dumps(step_def))
    canonical = step['properties'].pop('step_type')['const']
    for bookkeeping in ('position_in_experimental_plan', 'datetime', 'operator'):
        step['properties'].pop(bookkeeping, None)
    return {
        '$schema': full['$schema'],
        'type': 'object',
        'additionalProperties': False,
        'required': ['step_type', 'variants'],
        'properties': {
            'step_type': {'const': canonical},
            'variants': {'type': 'array', 'items': step},
        },
    }


def _strip_units(node):
    """Drop NOMAD `unit` keys anywhere in a schema subtree (not valid JSON Schema)."""
    if isinstance(node, dict):
        return {k: _strip_units(v) for k, v in node.items() if k != 'unit'}
    if isinstance(node, list):
        return [_strip_units(v) for v in node]
    return node


def _label(value: str) -> str:
    """Bare sample label: drop a leading "sample(s)" word the model sometimes
    echoes, so "sample 1" -> "1"."""
    return re.sub(r'^samples?\s+', '', str(value).strip(), flags=re.IGNORECASE)


def normalize_variants(slot: dict) -> dict:
    """Post-process one extracted slot in place: sample labels to bare form."""
    for variant in slot.get('variants', []):
        if isinstance(variant.get('samples'), list):
            variant['samples'] = [_label(x) for x in variant['samples']]
    return slot
