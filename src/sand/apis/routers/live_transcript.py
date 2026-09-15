"""Live transcription relay: browser audio -> Deepgram -> transcript events.

The browser cannot hold the Deepgram key, so it talks only to sand: it
streams MediaRecorder chunks over this WebSocket, sand forwards them to
Deepgram's live API and relays the transcript JSON back. The recording
itself is NOT stored here - the browser keeps the full blob and uploads
it through the normal /audio endpoint when recording stops.

Protocol (client side):
  1. connect, send {"token": "<NOMAD bearer token>"} as the first message
     (WebSockets cannot carry an Authorization header from a browser);
  2. wait for {"type": "ready"};
  3. send audio chunks as binary frames;
  4. send {"type": "stop"} (or just close) - sand tells Deepgram to
     flush, relays the remaining final transcripts, then closes.
Deepgram's Results messages are forwarded verbatim; the client reads
channel.alternatives[0].transcript and is_final.
"""

import asyncio
import json
from contextlib import suppress
from http import HTTPStatus

import websockets
from fastapi import APIRouter, WebSocket

router = APIRouter()

DEEPGRAM_LIVE_URL = 'wss://api.deepgram.com/v1/listen'

# How long to wait for Deepgram's remaining finals after CloseStream.
DRAIN_TIMEOUT_S = 10.0
AUTH_TIMEOUT_S = 10.0


async def _token_is_valid(app, token: str) -> bool:
    """One cheap NOMAD call; without this the relay would be an open
    proxy to a paid Deepgram account."""
    if not token:
        return False
    voice = app.state.voice_eln
    try:
        async with voice.build_client(token) as client:
            response = await client.get('/users/me')
    except Exception:
        return False
    return response.status_code == HTTPStatus.OK


async def _pump_client_audio(client_ws: WebSocket, deepgram) -> None:
    """Forward binary frames until the client stops or disconnects, then
    ask Deepgram to flush its final results."""
    while True:
        message = await client_ws.receive()
        if message.get('type') == 'websocket.disconnect':
            break
        if message.get('bytes'):
            await deepgram.send(message['bytes'])
            continue
        if message.get('text'):
            try:
                control = json.loads(message['text'])
            except ValueError:
                continue
            if control.get('type') == 'stop':
                break
    await deepgram.send(json.dumps({'type': 'CloseStream'}))


async def _pump_transcripts(deepgram, client_ws: WebSocket) -> None:
    """Forward Deepgram's JSON messages verbatim until it closes (it
    closes itself after CloseStream once all finals are delivered)."""
    async for message in deepgram:
        if isinstance(message, str):
            await client_ws.send_text(message)


@router.websocket('/live-transcript')
async def live_transcript(client_ws: WebSocket) -> None:
    app = client_ws.app
    await client_ws.accept()

    api_key = app.state.deepgram_api_key
    if not api_key:
        await client_ws.close(code=4503, reason='live transcription not configured')
        return

    try:
        first = await asyncio.wait_for(client_ws.receive_text(), timeout=AUTH_TIMEOUT_S)
        token = json.loads(first).get('token', '')
    except Exception:
        await client_ws.close(code=4401, reason='expected an auth message first')
        return
    if not await _token_is_valid(app, token):
        await client_ws.close(code=4401, reason='invalid NOMAD token')
        return

    url = f'{DEEPGRAM_LIVE_URL}?model={app.state.deepgram_model}&interim_results=true&smart_format=true'
    try:
        deepgram = await websockets.connect(
            url, additional_headers={'Authorization': f'Token {api_key}'}
        )
    except Exception:
        await client_ws.close(code=1011, reason='could not reach Deepgram')
        return

    await client_ws.send_text(json.dumps({'type': 'ready'}))
    up = asyncio.create_task(_pump_client_audio(client_ws, deepgram))
    down = asyncio.create_task(_pump_transcripts(deepgram, client_ws))
    try:
        await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        if up.done() and not down.done():
            # client finished: drain Deepgram's remaining finals
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(down, timeout=DRAIN_TIMEOUT_S)
    finally:
        up.cancel()
        down.cancel()
        with suppress(Exception):
            await deepgram.close()
        with suppress(Exception):
            await client_ws.close()
