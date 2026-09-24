// Live transcription while recording (best-effort): chunks stream to
// sand's Deepgram relay in parallel with the local accumulation; if the
// relay is off or fails, recording works unchanged.

import { authToken } from "./api.js";

// One object per relay connection: handlers close over it, so a socket
// that outlives its recording (draining finals) or a stale callback from
// a quickly-restarted recording can never touch the next recording's
// state. `liveConn` always points at the connection of the CURRENT
// recording; only that one may write to the panel or receive chunks.
let liveConn = null;

const liveTranscriptEl = document.getElementById("live-transcript");
const liveFinalEl = document.getElementById("live-final");
const liveInterimEl = document.getElementById("live-interim");

function liveTranscriptUrl() {
  const url = new URL("api/live-transcript", window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

export function clearLivePanel() {
  liveFinalEl.textContent = "";
  liveInterimEl.textContent = "";
  liveTranscriptEl.hidden = true;
}

export function startLiveTranscript() {
  clearLivePanel();
  let ws;
  try {
    ws = new WebSocket(liveTranscriptUrl());
  } catch (err) {
    return null;
  }
  const conn = {
    ws,
    ready: false,
    stopped: false,
    // MediaRecorder's FIRST chunk carries the WebM container header, so
    // chunks produced before the relay is ready are queued, not dropped.
    queue: [],
    // finals accumulate here (not read back from the DOM), so the value
    // resolved on close is this recording's text even if another
    // recording has taken over the panel meanwhile
    finals: "",
    finish: null,
    done: null,
  };
  conn.done = new Promise((resolve) => {
    conn.finish = resolve;
  });
  liveConn = conn;

  ws.onopen = () => ws.send(JSON.stringify({ token: authToken() }));
  ws.onmessage = (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    if (msg.type === "relay-ready") {
      if (liveConn !== conn) {
        // superseded by a newer recording: never touch its state
        ws.close();
        return;
      }
      conn.ready = true;
      liveTranscriptEl.hidden = false;
      for (const chunk of conn.queue) ws.send(chunk);
      conn.queue = [];
      // recording already stopped while we were connecting: the queued
      // chunks (header included) are sent above, now ask for the flush
      if (conn.stopped) ws.send(JSON.stringify({ type: "relay-stop" }));
      return;
    }
    const alt = msg.channel && msg.channel.alternatives && msg.channel.alternatives[0];
    if (!alt) return;
    if (msg.is_final) {
      if (alt.transcript) {
        conn.finals += (conn.finals ? " " : "") + alt.transcript;
      }
    }
    if (liveConn === conn) {
      // drain finals of a stopped recording still render, but a newer
      // recording owns the panel
      if (msg.is_final) {
        liveFinalEl.textContent = conn.finals;
        liveInterimEl.textContent = "";
      } else {
        liveInterimEl.textContent = alt.transcript || "";
      }
    }
  };
  // clean completion only: a close after a requested stop carries the
  // finals; an unexpected close resolves empty, so a half-dead stream
  // can never be saved as a complete transcript (whisper covers it)
  ws.onclose = () => conn.finish(conn.stopped ? conn.finals.trim() : "");
  return conn;
}

export function sendLiveChunk(chunk) {
  const conn = liveConn;
  if (!conn || conn.stopped) return;
  if (conn.ready && conn.ws.readyState === WebSocket.OPEN) {
    conn.ws.send(chunk);
  } else if (conn.ws.readyState !== WebSocket.CLOSED) {
    conn.queue.push(chunk);
  }
}

// Resolves with this recording's final transcript once the relay socket
// has closed - Deepgram's LAST finals arrive after the stop message, so
// reading any earlier would truncate the text. Without `conn`, stops the
// current recording's connection.
export function stopLiveTranscript(conn = liveConn, detach = false) {
  if (!conn) return Promise.resolve("");
  if (detach && liveConn === conn) liveConn = null;
  if (conn.stopped) return conn.done;
  conn.stopped = true;
  const ws = conn.ws;
  if (ws.readyState === WebSocket.OPEN && conn.ready) {
    // ask sand to flush Deepgram; the remaining finals arrive before close
    ws.send(JSON.stringify({ type: "relay-stop" }));
  }
  // not ready yet: keep the socket - the relay-ready handler flushes the
  // queued chunks and sends relay-stop itself. Either way, give up after
  // a deadline so the upload can never hang on a wedged socket.
  setTimeout(() => {
    if (ws.readyState !== WebSocket.CLOSED) ws.close();
    conn.finish(""); // a wedged socket is not a clean completion
  }, 12000);
  if (ws.readyState === WebSocket.CLOSED) conn.finish("");
  return conn.done;
}

// --- save-live-transcript toggle -----------------------------------
// The user decides per recording whether the live text is stored
// (whisper skipped) or display-only (whisper transcribes). The server
// config only sets the default; the last choice is remembered locally.

const storeLiveLabel = document.getElementById("store-live-label");
const storeLiveToggle = document.getElementById("store-live-toggle");
const STORE_LIVE_KEY = "sand.storeLiveTranscript";

export function storeLiveChosen() {
  return !storeLiveLabel.hidden && storeLiveToggle.checked;
}

export async function initLiveToggle() {
  storeLiveToggle.addEventListener("change", () => {
    try { localStorage.setItem(STORE_LIVE_KEY, String(storeLiveToggle.checked)); } catch { /* ignore */ }
  });
  try {
    const res = await fetch("ui-config");
    const cfg = await res.json();
    if (!cfg.live_transcript_available) return; // no relay: keep hidden
    let remembered = null;
    try { remembered = localStorage.getItem(STORE_LIVE_KEY); } catch { /* ignore */ }
    storeLiveToggle.checked =
      remembered === null ? Boolean(cfg.store_live_transcript) : remembered === "true";
    storeLiveLabel.hidden = false;
  } catch { /* toggle stays hidden; recording works without it */ }
}
