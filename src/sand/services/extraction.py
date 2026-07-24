import json
from pathlib import Path
from typing import Any

import httpx

SCHEMA_PATH = Path(__file__).parent.parent / 'flatten_HySprint_SlotDieCoating.json'

# Helmholtz Blablador, an OpenAI-compatible API:
# https://sdlaml.pages.jsc.fz-juelich.de/ai/guides/blablador_api_access/
BLABLADOR_BASE_URL = 'https://api.blablador.fz-juelich.de/v1'

SYSTEM_PROMPT = (
    'You are a world class AI that excels at extracting structured data about '
    'slot-die coating processes for thin-film samples (e.g. perovskite '
    'absorber layers) from lab notes and spoken process descriptions. You '
    'never come up with data: only report values that are explicitly stated '
    'in the text. It is better to leave a field out than to report data you '
    'are uncertain about. Do not derive values by doing maths or inference; '
    'stick to what is written. The one exception is units: schema fields '
    'carry a "unit" annotation, and every numeric value must be converted to '
    'that unit before reporting (e.g. for a field with unit "mm", 100 '
    'micrometers is reported as 0.1). Be careful with decimal points. Put '
    'each chemical of the coating solution in the correct list (solute, '
    'solvent, or additive) with its stated amount, volume, or concentration. '
    'Describe the coating parameters, quenching, annealing, and deposited '
    'layers in their dedicated sections, and keep data of separate solutions '
    'and steps apart. Only use the allowed types and literal values provided '
    'in the schema; if there are options, choose one. Some fields are marked '
    '"ENTRY REFERENCE" in their schema description (e.g. chemical, batch, '
    'samples): these hold references to other NOMAD database entries, so '
    'never put a chemical name or any other free text in them — omit them '
    'unless the text explicitly provides such a reference. Chemical names '
    'belong in the respective name fields. Keep to the given schema.'
)

INSTRUCTION_TEXT = (
    'Extract the slot-die coating process data from the following '
    'description. Fill only fields whose values are explicitly stated, and '
    'convert each numeric value to the unit specified for that field in the '
    'schema. Report the coating solution formulation (solutes, solvents, and '
    'additives with their names and amounts), the coating parameters (flow '
    'rate, head distance, coating speed, temperatures), any quenching step '
    '(e.g. air knife settings), the annealing conditions (temperature, time, '
    'atmosphere), and the deposited layer (type and material). If a name for '
    'the process or sample is stated, use it as the name.'
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

    async def extract(self, text: str) -> dict[str, Any]:
        user_message = (
            f'{INSTRUCTION_TEXT}\n\n'
            'Return the extracted data as a single JSON object that validates '
            'against the following JSON Schema. Output only the JSON object '
            '— no markdown, no explanations.\n\n'
            f'JSON Schema:\n{json.dumps(self._schema)}\n\n'
            f'Process description:\n{text}'
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

        return data if isinstance(data, dict) else {}

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
