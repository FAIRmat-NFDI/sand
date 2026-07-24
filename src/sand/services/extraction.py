import json
from pathlib import Path
from typing import Any

import httpx

SCHEMA_PATH = Path(__file__).parent.parent / 'solar_cell_schema.json'

# The google-genai SDK requires httpx>=0.28.1 while nomad-lab pins
# httpx<0.28, so the Gemini REST API is called directly via httpx.
GEMINI_BASE_URL = 'https://generativelanguage.googleapis.com/v1beta'

# Prompts adapted from lamalab-org/perla-extract
# (src/perla_extract/constants.py)
SYSTEM_PROMPT = (
    'You are a world class AI that excels at extracting data about perovskite '
    'solar cells from papers. You only report single junction solar cells and no '
    'other types of solar cells. You never come up with data and only state data '
    'that have been measured and written in the paper and which you can '
    'confidently extract. It is better for you to skip than to report data you '
    'are uncertain in. Take care to separate devices. Do not extract data people '
    'took from other papers but only data reported for the first time in this '
    'paper. Do not convert units yourself and stick to the units reported in the '
    'paper. Be careful with decimal points. Do not try to come up with a value by '
    'doing maths or any inference. Stick to what is explicitly written. Be careful '
    'that the data you put together really belongs to the same device. Do not '
    'forget to get all the different cells/devices. There can be many. You can '
    'make a guess for dimensionality. Make sure to only use the allowed types and '
    'literal values provided in the schema. If there are options, choose one. The '
    'device stack has to be listed separately in the layers section of the schema '
    'with layer names as the names of the parts of the stack. Do not miss the '
    'stack/layers. Make sure to separate deposition steps like thermal annealing '
    'and spin coating, etc. Keep to the given schema.'
)

INSTRUCTION_TEXT = (
    'Extract the data from the text of the paper. Only report data about devices '
    'for which you are certain that the extraction you provide is correct. Do not '
    'convert any value or unit. Do not forget to fill in the bandgap. Make sure it '
    'is correct for the cell to the best of your abilities. If you\'re not '
    'confident, skip it. Always fill the ions section and coefficients for the '
    'perovskite material. If it\'s not stated, you can infer it from the formula. '
    'For example, for MAPbI3 you get coefficients 1 for MA, 1 for Pb, and 3 for I.'
)


def _load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


class ExtractionService:
    def __init__(
        self,
        api_key: str,
        model: str = 'gemini-2.5-flash',
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=GEMINI_BASE_URL,
            headers={'x-goog-api-key': api_key},
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        self._model = model
        self._schema = _load_schema()

    async def extract(self, text: str) -> list[dict[str, Any]]:
        payload = {
            'systemInstruction': {'parts': [{'text': SYSTEM_PROMPT}]},
            'contents': [
                {
                    'role': 'user',
                    'parts': [{'text': f'{INSTRUCTION_TEXT}\n\n{text}'}],
                }
            ],
            'generationConfig': {
                'responseMimeType': 'application/json',
                'responseJsonSchema': self._schema,
            },
        }
        try:
            response = await self._client.post(
                f'/models/{self._model}:generateContent', json=payload
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f'Gemini API error ({exc.response.status_code}): '
                f'{exc.response.text}'
            ) from exc
        except Exception as exc:
            raise RuntimeError(f'Gemini API call failed: {exc}') from exc

        try:
            parts = response.json()['candidates'][0]['content']['parts']
            data = json.loads(''.join(part.get('text', '') for part in parts))
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f'Unexpected Gemini response: {exc}'
            ) from exc

        return data.get('cells') or []

    async def close(self) -> None:
        await self._client.aclose()
