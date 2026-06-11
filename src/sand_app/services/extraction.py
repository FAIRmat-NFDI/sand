import json
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic, APIStatusError


SCHEMA_PATH = Path(__file__).parent.parent / 'sollar_cell_schema.json'

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
    """Build the Anthropic tool from the PerovskiteSolarCells JSON Schema.

    The schema is already an object with a ``cells`` array of
    ``PerovskiteSolarCell`` entries, so it is used directly as the tool's
    ``input_schema``.
    """
    return {
        'name': 'record_perovskite_solar_cells',
        'description': (
            'Record every single-junction perovskite solar cell extracted from '
            'the text. Each device becomes a separate entry in the cells array.'
        ),
        'input_schema': cell_schema,
    }


class ExtractionService:
    def __init__(
        self,
        api_key: str,
        model: str = 'claude-sonnet-4-20250514',
    ) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        schema = _load_schema()
        self._tool = _build_tool(schema)

    async def extract(self, text: str) -> list[dict[str, Any]]:
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=16384,
                system=SYSTEM_PROMPT,
                tools=[self._tool],
                tool_choice={
                    'type': 'tool',
                    'name': 'record_perovskite_solar_cells',
                },
                messages=[
                    {'role': 'user', 'content': f'{INSTRUCTION_TEXT}\n\n{text}'}
                ],
            )
        except APIStatusError as exc:
            raise RuntimeError(
                f'Anthropic API error ({exc.status_code}): {exc.message}'
            ) from exc
        except Exception as exc:
            raise RuntimeError(f'Anthropic API call failed: {exc}') from exc

        for block in response.content:
            if (
                block.type == 'tool_use'
                and block.name == 'record_perovskite_solar_cells'
            ):
                return block.input.get('cells') or []

        raise RuntimeError(
            'Anthropic response did not contain expected tool_use block'
        )

    async def close(self) -> None:
        await self._client.close()
