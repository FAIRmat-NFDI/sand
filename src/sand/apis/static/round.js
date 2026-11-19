// The rounds being prepared (#208), one per experiment: the edits
// unticked (each with an optional reason) and the rejected ones ticked
// again. Nothing is sent before the recompute; kept in the page only, so a
// reload drops them (the page warns before leaving). Another experiment
// keeps its own round: switching sheets loses nothing.

const rounds = new Map(); // entry id -> {unticked, restored}
let entryId = null;
let unticked = new Map(); // key -> {reason, meant, entry, line}
let restored = new Map(); // key -> {entry, line}
const listeners = new Set();

function changed() {
  for (const fn of listeners) fn();
}

// the experiment the round is for; each keeps its own
export function roundFor(id) {
  if (id === entryId) return;
  if (entryId !== null) rounds.set(entryId, { unticked, restored });
  entryId = id;
  const kept = rounds.get(id);
  unticked = kept ? kept.unticked : new Map();
  restored = kept ? kept.restored : new Map();
  changed();
}

// any round not sent, of any experiment: the page warns before leaving
export function anyRound() {
  return roundSize() > 0 || [...rounds].some(([id, r]) => id !== entryId && r.unticked.size + r.restored.size > 0);
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

// after a round of experiment `id` was applied: what `body` sent goes,
// a change made while it was on its way stays for the next round (the
// page may show another experiment by then)
export function clearSent(body, id) {
  const round = id === entryId ? { unticked, restored } : rounds.get(id);
  if (!round) return;
  let gone = 0;
  for (const sent of body.reject) {
    const now = round.unticked.get(sent.key);
    if (now && now.reason === sent.reason && now.meant === sent.meant) gone += round.unticked.delete(sent.key);
  }
  for (const key of body.restore) gone += round.restored.delete(key);
  if (gone && id === entryId) changed();
}
