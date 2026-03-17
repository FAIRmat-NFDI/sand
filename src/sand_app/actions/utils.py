import json
from pathlib import Path

SYSTEM_PROMPT = (
    'You are a lab assistant that extracts structured process data from '
    "materials scientists' natural-language descriptions. The user will describe one or "
    'more lab processes. Extract each distinct process (e.g. cleaning, '
    'sputtering, annealing) as a separate entry. Use the record_processes tool '
    'to return the extracted data. Only include fields that are clearly stated '
    'or strongly implied by the narrative. Do not invent data.'
)


# Shared Schema and Tool Utils
def load_schema() -> dict:
    # Resolve the path relative to the sand-app package root
    path = Path(__file__).parent.parent / 'elnprocess_schema.json'
    if not path.exists():
        raise ValueError
    with open(path) as f:
        return json.load(f)
