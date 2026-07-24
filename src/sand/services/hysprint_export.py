"""Wrap LLM-extracted slot-die coating data as a NOMAD archive entry.

The extraction schema is the flattened NOMAD API export of the
``HySprint_SlotDieCoating`` class, so the filled data maps 1:1 onto that
section — no field conversion is needed, only wrapping it in an archive
dict with the right ``m_def``. The target NOMAD must have the
``nomad-hysprint`` plugin installed for the entry to be recognized.
"""

from typing import Any

M_DEF = 'nomad_hysprint.schema_packages.hysprint_package.HySprint_SlotDieCoating'

# Entry-reference quantities are proxy strings that NOMAD normalizers try to
# resolve; an LLM-invented value there ("PbI2") crashes entry processing.
# They are stripped from the extraction schema too, but the model is only
# prompt-constrained and can still emit them. 'solution' is a reference only
# when it holds a string (the solution subsection list must pass through).
REFERENCE_KEYS = frozenset(
    {'samples', 'instruments', 'steps', 'batch', 'chemical', 'reference'}
)


def prune_empty(value: Any) -> Any:
    """Recursively drop None values, empty dicts/lists, and reference keys.

    The LLM fills unstated schema fields with nulls; NOMAD expects absent
    quantities to be absent from the archive, not null.
    """
    if isinstance(value, dict):
        pruned = {
            k: prune_empty(v)
            for k, v in value.items()
            if k not in REFERENCE_KEYS
            and not (k == 'solution' and isinstance(v, str))
        }
        return {k: v for k, v in pruned.items() if v not in (None, {}, [])}
    if isinstance(value, list):
        pruned = [prune_empty(v) for v in value]
        return [v for v in pruned if v not in (None, {}, [])]
    return value


def build_archive(data: dict[str, Any]) -> dict[str, Any]:
    """Build a NOMAD archive dict from extracted slot-die coating data."""
    return {'data': {'m_def': M_DEF, **prune_empty(data)}}


def process_display_name(data: dict[str, Any]) -> str:
    """Human-readable name for a slot-die coating archive's data dict."""
    return data.get('name') or 'Slot-die coating process'
