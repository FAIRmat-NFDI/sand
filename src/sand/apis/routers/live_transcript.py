"""Live transcription relay: browser audio -> Deepgram -> transcript.

Protocol (client side):
  1. connect, send {"token": "<NOMAD bearer token>"} as the first message
     (an empty token: authenticate with NOMAD's Authorization cookie,
     which the browser sends with the handshake)
  2. wait for {"type": "relay-ready"};
  3. send audio chunks as binary frames;
  4. send {"type": "relay-stop"} (or just close) - sand tells Deepgram to
     flush, relays the remaining final transcripts, then closes.
Deepgram's Results messages are forwarded verbatim; the client reads
channel.alternatives[0].transcript and is_final.

        WebSocket #1            WebSocket #2
  browser ◀════════▶ sand backend ◀════════▶ api.deepgram.com
          browser_ws                 deepgram_ws

"""

import asyncio
import json
from contextlib import suppress
from http import HTTPStatus

import websockets
from fastapi import APIRouter, WebSocket

from sand.apis.deps import token_from_cookie

router = APIRouter()

DEEPGRAM_LIVE_URL = 'wss://api.deepgram.com/v1/listen'

# How long to wait for Deepgram's remaining finals after CloseStream.
DRAIN_TIMEOUT_S = 10.0
AUTH_TIMEOUT_S = 10.0


async def _token_is_valid(app, token: str) -> bool:
    """One NOMAD auth call to not open the transcript to public"""
    if not token:
        return False
    voice = app.state.voice_eln
    try:
        async with voice.build_client(token) as client:
            response = await client.get('/users/me')
    except Exception:
        return False
    return response.status_code == HTTPStatus.OK


# browser ──▶ Deepgram
async def _pump_client_audio(browser_ws: WebSocket, deepgram_ws) -> None:
    """Forward binary frames until the client stops or disconnects, then
    ask Deepgram to flush its final results."""
    while True:
        message = await browser_ws.receive()
        if message.get('type') == 'websocket.disconnect':
            break
        if message.get('bytes'):
            await deepgram_ws.send(message['bytes'])
            continue
        if message.get('text'):
            try:
                control = json.loads(message['text'])
            except ValueError:
                continue
            # the browser tells sand to stop -> tell Deepgram to flush
            if control.get('type') == 'relay-stop':
                break
    await deepgram_ws.send(json.dumps({'type': 'CloseStream'}))


# Deepgram ──▶ browser
async def _pump_transcripts(deepgram_ws, browser_ws: WebSocket) -> None:
    """Forward Deepgram's JSON messages until it closes (it
    closes itself after CloseStream once all finals are delivered)."""
    async for message in deepgram_ws:
        if isinstance(message, str):
            await browser_ws.send_text(message)


@router.websocket('/live-transcript')
async def live_transcript(browser_ws: WebSocket) -> None:
    app = browser_ws.app
    await browser_ws.accept()

    api_key = app.state.deepgram_api_key
    if not api_key:
        await browser_ws.close(code=4503, reason='live transcription not configured')
        return

    try:
        first = await asyncio.wait_for(
            browser_ws.receive_text(), timeout=AUTH_TIMEOUT_S
        )
        token = json.loads(first).get('token', '') or token_from_cookie(
            browser_ws.cookies
        )
    except Exception:
        await browser_ws.close(code=4401, reason='expected an auth message first')
        return
    if not await _token_is_valid(app, token):
        await browser_ws.close(code=4401, reason='invalid NOMAD token')
        return

    url = f'{DEEPGRAM_LIVE_URL}?model={app.state.deepgram_model}&interim_results=true&smart_format=true'
    try:
        deepgram_ws = await websockets.connect(
            url, additional_headers={'Authorization': f'Token {api_key}'}
        )
    except Exception:
        await browser_ws.close(code=1011, reason='could not reach Deepgram')
        return

    # From here on the paid Deepgram socket is open: every await (the
    # ready-send included) can raise on a browser disconnect, so it all
    # runs inside the scope whose finally closes both sockets.
    up = down = None
    try:
        # sand tells the browser it is ready to relay audio
        await browser_ws.send_text(json.dumps({'type': 'relay-ready'}))
        up = asyncio.create_task(_pump_client_audio(browser_ws, deepgram_ws))
        down = asyncio.create_task(_pump_transcripts(deepgram_ws, browser_ws))
        await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        if up.done() and not down.done():
            # client finished: drain Deepgram's remaining finals
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(down, timeout=DRAIN_TIMEOUT_S)
    finally:
        if up is not None:
            up.cancel()
        if down is not None:
            down.cancel()
        with suppress(Exception):
            await deepgram_ws.close()
        with suppress(Exception):
            await browser_ws.close()
