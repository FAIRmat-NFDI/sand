import json
import os
from pathlib import Path

from groq import APIConnectionError, APIError, AsyncGroq
from groq.types.chat import ChatCompletionToolParam
from groq.types.shared_params import FunctionDefinition
from nomad.actions.assets import resolve_action_asset_path
from nomad.processing.data import Upload
from temporalio import activity

from sand_app.actions.remote_sand.models import RemoteSandWorkflowInput
from sand_app.actions.utils import SYSTEM_PROMPT, load_schema

GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')


def build_tool(eln_schema: dict) -> ChatCompletionToolParam:
    return ChatCompletionToolParam(
        type='function',
        function=FunctionDefinition(
            name='record_processes',
            description=(
                "Record one or more lab processes extracted from the user's "
                'narrative. Each process becomes a separate ELNProcess entry.'
            ),
            parameters={
                'type': 'object',
                'properties': {
                    'processes': {
                        'type': 'array',
                        'items': eln_schema,
                    }
                },
                'required': ['processes'],
            },
        ),
    )


@activity.defn
async def remote_sst(data: RemoteSandWorkflowInput) -> str:
    """Speech-to-Text activity using Groq API"""
    if not GROQ_API_KEY:
        raise RuntimeError('GROQ_API_KEY environment variable is not set.')

    client = AsyncGroq(api_key=GROQ_API_KEY)
    model = 'whisper-large-v3'

    file_path = await resolve_action_asset_path(data.audio_file, data.user_id)
    filename = Path(file_path).name

    with open(file_path, 'rb') as f:
        audio_bytes = f.read()

    try:
        response = await client.audio.transcriptions.create(
            file=(filename, audio_bytes),
            model=model,
            response_format='text',
        )
    except APIConnectionError as exc:
        raise RuntimeError(f'Groq connection failed: {exc}') from exc
    except APIError as exc:
        raise RuntimeError(
            f'Groq transcription failed ({exc.status_code}): {exc.message}'
        ) from exc
    except Exception as exc:
        raise RuntimeError(f'Groq transcription failed: {exc}') from exc
    finally:
        await client.close()

    return str(response).strip()


@activity.defn
async def remote_schema_extraction(verified_text: str, model: str) -> list[dict]:
    """Schema Extraction activity using a configurable model via Groq"""
    if not GROQ_API_KEY:
        raise RuntimeError('GROQ_API_KEY environment variable is not set.')

    client = AsyncGroq(api_key=GROQ_API_KEY)

    schema = load_schema()
    tool = build_tool(schema)

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': verified_text},
            ],
            tools=[tool],
            tool_choice={'type': 'function', 'function': {'name': 'record_processes'}},
            max_tokens=4096,
        )
    except Exception as exc:
        raise RuntimeError(f'Groq completion failed: {exc}') from exc
    finally:
        await client.close()

    choice = response.choices[0]
    if choice.message.tool_calls:
        for tool_call in choice.message.tool_calls:
            if tool_call.function.name == 'record_processes':
                try:
                    args = json.loads(tool_call.function.arguments)
                    return args.get('processes', [])
                except json.JSONDecodeError:
                    raise RuntimeError('Failed to decode tool arguments')

    raise RuntimeError('Groq response did not contain expected tool_call block')


@activity.defn
async def remote_upload_entry_activity(
    schema_data: list[dict], upload_id: str, user_id: str
) -> str:
    """Common activity to upload extracted process data to NOMAD as archive files and process them."""
    from sand_app.services.archive import build_archive

    upload = Upload.get(upload_id)
    staging_files = upload.staging_upload_files

    for idx, process in enumerate(schema_data):
        name = process.get('name', f'process_{idx}')
        # Sanitize filename: replace spaces and slashes
        safe_name = name.replace(' ', '_').replace('/', '_')
        filename = f'{safe_name}.archive.json'

        archive_dict = build_archive(process)
        with staging_files.raw_file(filename, 'wt') as f:
            json.dump(archive_dict, f)

    # Process all the newly added files and wait for completion
    handle = upload.process_upload()
    await handle.result()

    return f'Successfully processed {len(schema_data)} entries in upload {upload_id}'
