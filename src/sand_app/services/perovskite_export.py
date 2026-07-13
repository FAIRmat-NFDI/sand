"""Prepare LLM-extracted perovskite cells for NOMAD upload.

Thin adapter over the upstream **perla-extract** package so sand-app stays in
sync with it instead of duplicating its logic. The heavy lifting — unit
canonicalization (``postprocess``), PCE/hallucination filtering, key renaming,
{value, unit} flattening, ``layer_order`` derivation, and one-entry-per-cell
splitting — all lives in perla-extract:
- ``perla_extract.postprocessing.postprocess``
- ``perla_extract.export.convert_extraction_to_nomad_entries``

This module is the single integration point: if the perla-extract dependency
ever needs to be swapped back for a local port, only this file changes.
"""

from typing import Any

from perla_extract.export import convert_extraction_to_nomad_entries
from perla_extract.postprocessing import postprocess
from perla_extract.pydantic_model_reduced import PerovskiteSolarCells


def convert_cells_to_nomad_entries(
    cells: list[dict[str, Any]],
    source_text: str = '',
    doi: str = '',
    ureg=None,
) -> list[dict[str, Any]]:
    """Postprocess, validate, filter, and split cells into NOMAD archive entries.

    Mirrors perla-extract's pipeline: ``postprocess`` the raw extraction, validate
    it against ``PerovskiteSolarCells``, then run
    ``convert_extraction_to_nomad_entries`` (filter + transform + split). Returns
    one ``{"data": {...}}`` archive dict per surviving cell, ready to upload.

    ``source_text`` is the extraction source (the user's text); it backs the
    hallucination guard, which drops numeric values not present in it — so it
    must be the real text, not empty.
    """
    model = PerovskiteSolarCells(**postprocess({'cells': cells or []}))
    entries = convert_extraction_to_nomad_entries(model, doi, source_text, ureg=ureg)
    if not doi:
        # convert_extraction_to_nomad_entries always stamps DOI_number; with no
        # real DOI it becomes "https://www.doi.org/", which the plugin's Ref
        # normalizer treats as truthy and tries to resolve via crossref, crashing
        # on the non-JSON response. Drop it so the `if self.DOI_number:` guard
        # skips the lookup entirely.
        for entry in entries:
            entry.get('data', {}).pop('DOI_number', None)
    return entries


def cell_display_name(data: dict[str, Any]) -> str:
    """Human-readable name for a cell archive's data dict."""
    composition = data.get('perovskite_composition') or {}
    formula = composition.get('formula')
    architecture = data.get('device_architecture')
    if formula and architecture:
        return f'{formula} ({architecture})'
    return formula or architecture or 'Perovskite solar cell'
