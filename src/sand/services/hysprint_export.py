"""Wrap LLM-extracted slot-die coating data as a NOMAD archive entry.

The extraction schema is the flattened NOMAD API export of the
``HySprint_SlotDieCoating`` class, so the filled data maps 1:1 onto that
section — no field conversion is needed, only wrapping it in an archive
dict with the right ``m_def``. The target NOMAD must have the
``nomad-hysprint`` plugin installed for the entry to be recognized.
"""

from typing import Any

M_DEF = 'nomad_hysprint.schema_packages.hysprint_package.HySprint_SlotDieCoating'


def prune_empty(value: Any) -> Any:
    """Recursively drop None values, empty dicts, and empty lists.

    The LLM fills unstated schema fields with nulls; NOMAD expects absent
    quantities to be absent from the archive, not null.
    """
    if isinstance(value, dict):
        pruned = {k: prune_empty(v) for k, v in value.items()}
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
