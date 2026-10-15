"""Live transcription relay: browser audio -> self-hosted Whisper -> transcript.

Protocol (client side):
  1. connect: the browser sends NOMAD's Authorization cookie with the
     handshake, which authenticates the socket;
  2. wait for {"type": "relay-ready"};
  3. send audio chunks as binary frames;
  4. send {"type": "relay-stop"} (or just close) - sand tells the server to
     flush, relays the remaining final transcripts, then closes.
The server is WhisperLiveKit, whose /v1/listen speaks Deepgram's live
protocol. Its Results messages are forwarded verbatim; the client reads
channel.alternatives[0].transcript and is_final.

        WebSocket #1            WebSocket #2
  browser ◀════════▶ sand backend ◀════════▶ WhisperLiveKit
          browser_ws                 stt_ws

"""

import asyncio
import json
from contextlib import suppress
from http import HTTPStatus

import websockets
from fastapi import APIRouter, WebSocket

from sand.apis.deps import token_from_cookie
from sand.services.live_transcript import live_transcript_url

router = APIRouter()

# How long to wait for the remaining finals after CloseStream. Whisper
# finishes the buffered audio only then, which takes longer than Deepgram
# did. Keep below the browser's stop deadline in live-transcript.js.
DRAIN_TIMEOUT_S = 30.0


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


# browser ──▶ server
async def _pump_client_audio(browser_ws: WebSocket, stt_ws) -> None:
    """Forward binary frames until the client stops or disconnects, then
    ask the server to flush its final results."""
    while True:
        message = await browser_ws.receive()
        if message.get('type') == 'websocket.disconnect':
            break
        if message.get('bytes'):
            await stt_ws.send(message['bytes'])
            continue
        if message.get('text'):
            try:
                control = json.loads(message['text'])
            except ValueError:
                continue
            # the browser tells sand to stop -> tell the server to flush
            if control.get('type') == 'relay-stop':
                break
    await stt_ws.send(json.dumps({'type': 'CloseStream'}))


# server ──▶ browser
async def _pump_transcripts(stt_ws, browser_ws: WebSocket) -> None:
    """Forward the server's JSON messages until it closes (it
    closes itself after CloseStream once all finals are delivered)."""
    async for message in stt_ws:
        if isinstance(message, str):
            await browser_ws.send_text(message)


@router.websocket('/live-transcript')
async def live_transcript(browser_ws: WebSocket) -> None:
    app = browser_ws.app
    await browser_ws.accept()

    base_url = app.state.live_transcript_url
    if not base_url:
        await browser_ws.close(code=4503, reason='live transcription not configured')
        return

    token = token_from_cookie(browser_ws.cookies)
    if not await _token_is_valid(app, token):
        await browser_ws.close(code=4401, reason='invalid NOMAD token')
        return

    api_key = app.state.live_transcript_api_key
    headers = {'Authorization': f'Token {api_key}'} if api_key else {}
    try:
        stt_ws = await websockets.connect(
            live_transcript_url(base_url), additional_headers=headers
        )
    except Exception:
        await browser_ws.close(
            code=1011, reason='could not reach the transcription server'
        )
        return

    # From here on the server socket is open: every await (the ready-send
    # included) can raise on a browser disconnect, so it all runs inside
    # the scope whose finally closes both sockets.
    up = down = None
    try:
        # sand tells the browser it is ready to relay audio
        await browser_ws.send_text(json.dumps({'type': 'relay-ready'}))
        up = asyncio.create_task(_pump_client_audio(browser_ws, stt_ws))
        down = asyncio.create_task(_pump_transcripts(stt_ws, browser_ws))
        await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        if up.done() and not down.done():
            # client finished: drain the server's remaining finals
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(down, timeout=DRAIN_TIMEOUT_S)
    finally:
        if up is not None:
            up.cancel()
        if down is not None:
            down.cancel()
        with suppress(Exception):
            await stt_ws.close()
        with suppress(Exception):
            await browser_ws.close()
