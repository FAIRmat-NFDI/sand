"""Route an experiment's collected inputs and assemble the final archive."""

import json
import re

from sand.hysprint.archive import build_samples, canonicalize, compose_experiment
from sand.services.voice_eln import EXPERIMENT_INFO_LABEL, CollectedInput

_TRAILING_NUMBER_RE = re.compile(r'(\d+)$')

REQUIRED_INFO_FIELDS = (
    'project_name',
    'batch',
    'subbatch',
    'first_sample',
    'n_samples',
)


class HysprintInputError(ValueError):
    """The collection's inputs cannot feed hysprint generation."""


def route_inputs(inputs: list[CollectedInput]) -> tuple[dict, list[str]]:
    """(experiment-info form, ordered step texts) from the collected inputs.

    The input labeled 'experiment_info' carries the form as JSON and is not
    a step; every other input is a step narration (an experiment may have
    none). An input without text (audio not transcribed / entry not
    processed) makes generation impossible, so it raises rather than being
    silently dropped.
    """
    info = None
    steps: list[str] = []
    for item in inputs:
        if item.label == EXPERIMENT_INFO_LABEL:
            if info is not None:
                raise HysprintInputError(
                    'more than one experiment_info input in this collection'
                )
            if item.text is None:
                raise HysprintInputError(
                    f'experiment_info input {item.entry_id} has no text'
                )
            try:
                info = json.loads(item.text)
            except ValueError:
                raise HysprintInputError(
                    f'experiment_info input {item.entry_id} is not valid JSON'
                )
            continue
        if item.text is None:
            raise HysprintInputError(
                f'{item.kind} input {item.entry_id} has no text yet '
                '(not transcribed or not processed)'
            )
        steps.append(item.text)

    if info is None:
        raise HysprintInputError(
            "no input labeled 'experiment_info' in this collection"
        )
    if not isinstance(info, dict):
        raise HysprintInputError('the experiment_info JSON must be an object')
    missing = [field for field in REQUIRED_INFO_FIELDS if not info.get(field)]
    if missing:
        raise HysprintInputError(
            f'experiment_info is missing fields: {", ".join(missing)}'
        )
    # the form JSON may carry n_samples as "3" or 3.0; downstream does range(n)
    try:
        info['n_samples'] = int(info['n_samples'])
    except (TypeError, ValueError):
        raise HysprintInputError('n_samples must be a whole number')
    if info['n_samples'] < 1:
        raise HysprintInputError('n_samples must be at least 1')
    return info, steps


def resolve_sample_labels(slot: dict, sample_names: list[str]) -> dict:
    """Map extracted sample labels to the declared sample names, in place.

    The narration may name samples differently from the form-generated
    names ('1' spoken, 's_1' declared): the model transcribes labels
    exactly as stated, so the mapping is code's job. Exact match first,
    else the unique declared name sharing the trailing number; anything
    else raises with the declared names listed. Trailing numbers compare
    as integers, so zero-padding never matters ('01', '001', 's_001' all
    resolve against 's_01' or 's_1' alike).
    """
    by_number: dict[int, list[str]] = {}
    for name in sample_names:
        m = _TRAILING_NUMBER_RE.search(name)
        if m:
            by_number.setdefault(int(m.group(1)), []).append(name)

    for variant in slot.get('variants', []):
        labels = variant.get('samples')
        if labels == 'all' or not isinstance(labels, list):
            continue
        resolved = []
        for label in labels:
            if label in sample_names:
                resolved.append(label)
                continue
            m = _TRAILING_NUMBER_RE.search(str(label))
            candidates = by_number.get(int(m.group(1)), []) if m else []
            if len(candidates) != 1:
                raise HysprintInputError(
                    f'cannot match the narrated sample {label!r} to one of the '
                    f'declared samples {sample_names}'
                )
            resolved.append(candidates[0])
        variant['samples'] = resolved
    return slot


def assemble(info: dict, slots: list[dict]) -> dict:
    """Ordered extracted slots + form -> the canonical {samples, steps} archive."""
    sample_names = [s['sample'] for s in build_samples(info)]
    slots = [resolve_sample_labels(slot, sample_names) for slot in slots]
    return canonicalize(compose_experiment(info, slots))
