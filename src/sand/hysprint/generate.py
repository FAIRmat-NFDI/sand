"""Route an experiment's collected inputs and assemble the final archive."""

import json

from sand.hysprint.archive import canonicalize, compose_experiment
from sand.services.voice_eln import EXPERIMENT_INFO_LABEL, CollectedInput

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

    The input labeled 'experiment_info' carries the form as JSON and is not a
    step; every other input is a step narration. An input without text (audio
    not transcribed / entry not processed) makes generation impossible, so it
    raises rather than being silently dropped.
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
    if not steps:
        raise HysprintInputError('the collection has no step inputs')
    return info, steps


def assemble(info: dict, slots: list[dict]) -> dict:
    """Ordered extracted slots + form -> the canonical {samples, steps} archive."""
    return canonicalize(compose_experiment(info, slots))
