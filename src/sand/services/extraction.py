import json
from pathlib import Path
from typing import Any

from groq import APIConnectionError, APIError, AsyncGroq

SCHEMA_PATH = Path(__file__).parent.parent / 'solar_cell_schema.json'

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


def _build_tool(cell_schema: dict[str, Any]) -> dict[str, Any]:
    """Build the tool definition from the PerovskiteSolarCells JSON Schema.

    The schema is already an object with a ``cells`` array of
    ``PerovskiteSolarCell`` entries, so it is used directly as the tool's
    ``parameters``.
    """
    return {
        'type': 'function',
        'function': {
            'name': 'record_perovskite_solar_cells',
            'description': (
                'Record every single-junction perovskite solar cell extracted '
                'from the text. Each device becomes a separate entry in the '
                'cells array.'
            ),
            'parameters': cell_schema,
        },
    }


class ExtractionService:
    def __init__(
        self,
        api_key: str,
        model: str = 'openai/gpt-oss-120b',
    ) -> None:
        self._client = AsyncGroq(api_key=api_key)
        self._model = model
        schema = _load_schema()
        self._tool = _build_tool(schema)

    async def extract(self, text: str) -> list[dict[str, Any]]:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                max_completion_tokens=16384,
                tools=[self._tool],
                tool_choice={
                    'type': 'function',
                    'function': {'name': 'record_perovskite_solar_cells'},
                },
                messages=[
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user', 'content': f'{INSTRUCTION_TEXT}\n\n{text}'},
                ],
            )
        except APIConnectionError as exc:
            raise RuntimeError(f'Groq connection failed: {exc}') from exc
        except APIError as exc:
            raise RuntimeError(
                f'Groq API error ({exc.status_code}): {exc.message}'
            ) from exc
        except Exception as exc:
            raise RuntimeError(f'Groq API call failed: {exc}') from exc

        for call in response.choices[0].message.tool_calls or []:
            if call.function.name == 'record_perovskite_solar_cells':
                try:
                    arguments = json.loads(call.function.arguments)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f'Groq tool call returned invalid JSON: {exc}'
                    ) from exc
                return arguments.get('cells') or []

        raise RuntimeError(
            'Groq response did not contain expected tool call'
        )

    async def close(self) -> None:
        await self._client.close()
