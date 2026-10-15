// Joining the live transcript's finals. No imports and no page: tested
// in node (tests/js).

// The server sends punctuation as finals of its own ("," then "ask"):
// those attach to the text before them without a space.
export function joinFinal(text, final) {
  if (!text) return final;
  return /^[,.;:!?]/.test(final) ? text + final : text + " " + final;
}
