import json
from pathlib import Path
from typing import Any

import httpx

SCHEMA_PATH = Path(__file__).parent.parent / 'solar_cell_schema.json'

# Helmholtz Blablador, an OpenAI-compatible API:
# https://sdlaml.pages.jsc.fz-juelich.de/ai/guides/blablador_api_access/
BLABLADOR_BASE_URL = 'https://api.blablador.fz-juelich.de/v1'

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


def _parse_json_reply(content: str) -> dict[str, Any]:
    """Extract the JSON object from a model reply.

    Tolerates markdown code fences and surrounding prose, since the API
    offers no enforced structured output.
    """
    start = content.find('{')
    end = content.rfind('}')
    if start == -1 or end <= start:
        raise RuntimeError('Model reply contains no JSON object')
    return json.loads(content[start : end + 1])


class ExtractionService:
    def __init__(
        self,
        api_key: str,
        model: str = 'alias-large',
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=BLABLADOR_BASE_URL,
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        self._model = model
        self._schema = _load_schema()

    async def extract(self, text: str) -> list[dict[str, Any]]:
        user_message = (
            f'{INSTRUCTION_TEXT}\n\n'
            'Return the extracted data as a single JSON object that validates '
            'against the following JSON Schema (a top-level object with a '
            '"cells" array, one entry per device). Output only the JSON '
            'object — no markdown, no explanations.\n\n'
            f'JSON Schema:\n{json.dumps(self._schema)}\n\n'
            f'Paper text:\n{text}'
        )
        payload = {
            'model': self._model,
            'max_tokens': 16384,
            'temperature': 0,
            # Stream so the connection is never silent for minutes (proxies
            # in front of Blablador drop quiet long-running requests).
            'stream': True,
            # alias-large (Qwen) is a reasoning model whose thinking eats the
            # token budget while message.content stays null; turn it off.
            'chat_template_kwargs': {'enable_thinking': False},
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': user_message},
            ],
        }
        try:
            content = await self._stream_completion(payload)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                # Deployment may not accept chat_template_kwargs; retry once
                # without it.
                retry = {
                    k: v for k, v in payload.items()
                    if k != 'chat_template_kwargs'
                }
                try:
                    content = await self._stream_completion(retry)
                except httpx.HTTPStatusError as retry_exc:
                    raise RuntimeError(
                        f'Blablador API error '
                        f'({retry_exc.response.status_code}): '
                        f'{retry_exc.response.text}'
                    ) from retry_exc
            else:
                raise RuntimeError(
                    f'Blablador API error ({exc.response.status_code}): '
                    f'{exc.response.text}'
                ) from exc
        except Exception as exc:
            raise RuntimeError(f'Blablador API call failed: {exc}') from exc

        if not content.strip():
            raise RuntimeError(
                'Blablador returned no content (the model may have spent the '
                'whole token budget on reasoning)'
            )
        try:
            data = _parse_json_reply(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f'Blablador reply was not valid JSON: {exc}'
            ) from exc

        return data.get('cells') or []

    async def _stream_completion(self, payload: dict[str, Any]) -> str:
        """Run a streaming chat completion and return the joined content."""
        parts: list[str] = []
        async with self._client.stream(
            'POST', '/chat/completions', json=payload
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith('data:'):
                    continue
                chunk = line[len('data:'):].strip()
                if chunk == '[DONE]':
                    break
                choices = json.loads(chunk).get('choices') or []
                if not choices:
                    continue
                piece = (choices[0].get('delta') or {}).get('content')
                if piece:
                    parts.append(piece)
        return ''.join(parts)

    async def close(self) -> None:
        await self._client.aclose()
