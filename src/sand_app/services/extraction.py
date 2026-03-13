import json
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic, APIStatusError


SCHEMA_PATH = Path(__file__).parent.parent / 'elnprocess_schema.json'

SYSTEM_PROMPT = (
    'You are a lab assistant that extracts structured process data from '
    "materials scientists' natural-language descriptions. The user will describe one or "
    'more lab processes. Extract each distinct process (e.g. cleaning, '
    'sputtering, annealing) as a separate entry. Use the record_processes tool '
    'to return the extracted data. Only include fields that are clearly stated '
    'or strongly implied by the narrative. Do not invent data.'
)


def _load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def _build_tool(eln_schema: dict[str, Any]) -> dict[str, Any]:
    """Build the Anthropic tool definition wrapping ELNProcess in an array."""
    return {
        'name': 'record_processes',
        'description': (
            "Record one or more lab processes extracted from the user's "
            'narrative. Each process becomes a separate ELNProcess entry.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'processes': {
                    'type': 'array',
                    'items': eln_schema,
                }
            },
            'required': ['processes'],
        },
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
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=[self._tool],
                tool_choice={'type': 'tool', 'name': 'record_processes'},
                messages=[{'role': 'user', 'content': text}],
            )
        except APIStatusError as exc:
            raise RuntimeError(
                f'Anthropic API error ({exc.status_code}): {exc.message}'
            ) from exc
        except Exception as exc:
            raise RuntimeError(f'Anthropic API call failed: {exc}') from exc

        for block in response.content:
            if block.type == 'tool_use' and block.name == 'record_processes':
                return block.input['processes']

        raise RuntimeError(
            'Anthropic response did not contain expected tool_use block'
        )

    async def close(self) -> None:
        await self._client.close()
