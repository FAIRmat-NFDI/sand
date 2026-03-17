import asyncio
import json
import os
import subprocess
import sys
import time

import ollama
import requests
from nomad.actions.assets import resolve_action_asset_path
from nomad.processing.data import Upload
from temporalio import activity

from sand_app.actions.local_sand.models import LocalSandWorkflowInput
from sand_app.actions.utils import SYSTEM_PROMPT, load_schema


# STT Logic (Dynamic platform selection)
def _transcribe_audio(audio_path: str) -> str:
    """Helper to transcribe audio using local models based on platform."""
    if sys.platform == 'darwin':
        try:
            import mlx_whisper

            activity.logger.info('Using mlx-whisper for local transcription.')
            result = mlx_whisper.transcribe(audio_path)
            return result.get('text', '')
        except ImportError:
            pass

    # Fallback to faster-whisper
    try:
        from faster_whisper import WhisperModel

        activity.logger.info('Using faster-whisper for local transcription.')
        model_size = 'base'
        model = WhisperModel(model_size, device='cpu', compute_type='int8')
        segments, _ = model.transcribe(audio_path, beam_size=5)
        return ' '.join([segment.text for segment in segments])
    except ImportError:
        raise RuntimeError(
            'No local transcription library (mlx-whisper or faster-whisper) found.'
        )


@activity.defn
async def local_sst(data: LocalSandWorkflowInput) -> str:
    """Local Speech-to-Text activity."""
    # Resolve the ActionAssetRef to a local absolute path
    audio_path = await resolve_action_asset_path(data.audio_file, data.user_id)

    if not audio_path or not os.path.exists(audio_path):
        raise RuntimeError(f'Resolved audio path does not exist: {audio_path}')

    result = await asyncio.to_thread(_transcribe_audio, str(audio_path))
    return result


# LLM Logic (Ollama subprocess)
@activity.defn
def local_schema_extraction(verified_text: str) -> list[dict]:
    """Local Schema Extraction using Ollama (Qwen:4b)."""

    # 1. Start Ollama server if not running
    # Note: In a production setting, this might be a long-lived service.
    # Here we follow the requested sync subprocess strategy.

    ollama_proc = None
    try:
        # Check if ollama is already reachable
        try:
            requests.get('http://localhost:11434/api/tags', timeout=1)
            activity.logger.info('Ollama server already running.')
        except requests.exceptions.ConnectionError:
            activity.logger.info('Starting Ollama server...')
            ollama_proc = subprocess.Popen(
                ['ollama', 'serve'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Wait for it to pull up
            for _ in range(30):
                time.sleep(1)
                try:
                    requests.get('http://localhost:11434/api/tags', timeout=1)
                    break
                except:
                    continue

        schema = load_schema()
        tool = {
            'type': 'function',
            'function': {
                'name': 'record_processes',
                'description': (
                    "Record one or more lab processes extracted from the user's "
                    'narrative. Each process becomes a separate ELNProcess entry.'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'processes': {
                            'type': 'array',
                            'items': schema,
                        }
                    },
                    'required': ['processes'],
                },
            },
        }

        response = ollama.chat(
            model='qwen3:14b',
            messages=[
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': verified_text},
            ],
            tools=[tool],
        )

        message = response['message']
        tool_calls = message.get('tool_calls') or []

        for tc in tool_calls:
            if tc['function']['name'] == 'record_processes':
                args = tc['function']['arguments']
                # ollama native client already parses arguments as a dict
                if isinstance(args, str):
                    args = json.loads(args)
                return args.get('processes', [])

        # Fallback: log if model replied with text instead of tool call
        if message.get('content'):
            activity.logger.warning('Using faster-whisper for local transcription.')
            activity.logger.warning(message['content'])

        return []
    finally:
        if ollama_proc:
            activity.logger.info('Stopping Ollama server.')
            ollama_proc.terminate()
            ollama_proc.wait()


@activity.defn
async def local_upload_entry_activity(
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
