// The round being prepared (#208), of one experiment: the edits unticked
// (each with an optional reason) and the rejected ones ticked again.
// Nothing is sent before Recompute; kept in the page only, so a reload
// drops it (the page warns before leaving).

let entryId = null;
let unticked = new Map(); // key -> {reason, meant, entry, line}
let restored = new Map(); // key -> {entry, line}
const listeners = new Set();

function changed() {
  for (const fn of listeners) fn();
}

// the experiment the round is for: another one starts an empty round
export function roundFor(id) {
  if (id === entryId) return;
  entryId = id;
  unticked = new Map();
  restored = new Map();
  changed();
}

export function onRoundChange(fn) {
  listeners.add(fn);
}

// `edit`: {key, line}, of the input `entry`
export function untick(edit, entry, why = { reason: null, meant: "" }) {
  unticked.set(edit.key, { reason: why.reason || null, meant: why.meant || "", entry, line: edit.line });
  changed();
}

export function tick(key) {
  if (unticked.delete(key)) changed();
}

export function restore(edit, entry) {
  restored.set(edit.key, { entry, line: edit.line });
  changed();
}

export function keepRejected(key) {
  if (restored.delete(key)) changed();
}

export function isUnticked(key) {
  return unticked.has(key);
}

export function isRestored(key) {
  return restored.has(key);
}

export function untickReason(key) {
  return unticked.get(key) || null;
}

export function roundSize() {
  return unticked.size + restored.size;
}

// the body of POST .../edits/round
export function roundBody() {
  return {
    reject: [...unticked].map(([key, why]) => ({ key, reason: why.reason, meant: why.meant })),
    restore: [...restored.keys()],
  };
}

// the unticks sent, to tell after the reread which no longer apply
export function sentUnticks() {
  return [...unticked].map(([key, why]) => ({ key, entry: why.entry, line: why.line }));
}

export function clearRound() {
  if (!roundSize()) return;
  unticked = new Map();
  restored = new Map();
  changed();
}
