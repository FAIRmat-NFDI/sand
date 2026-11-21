// The relay's address, with the open sheet's key terms: Deepgram takes
// them when sand connects to it, so they travel with the handshake.
export function liveTranscriptUrl(pageUrl, keyterms) {
  const url = new URL("api/live-transcript", pageUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  for (const term of keyterms) url.searchParams.append("keyterm", term);
  return url.toString();
}
