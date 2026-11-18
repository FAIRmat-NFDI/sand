// Edit mode's pure logic: what the Sheet card and the input tiles show
// for a job status. No DOM, so it runs under node's test runner.

export function describeEditPhase(status) {
  switch (status.phase) {
    case "starting":
      return "Starting...";
    case "collecting":
      return "Collecting the updates...";
    case "replaying":
      return "Checking the earlier updates...";
    case "interpreting": {
      // counted over the updates the model is asked about: the earlier
      // ones were replayed from their saved answers
      const replayed = status.inputs_replayed ?? 0;
      const total = (status.inputs_total ?? 0) - replayed;
      if (replayed && !total) return "Checking the earlier updates...";
      const current = Math.min((status.inputs_done ?? 0) - replayed + 1, total || 1);
      return "Reading update " + current + " of " + (total || "?") + "...";
    }
    case "writing-sheet":
      return "Writing the sheet...";
    default:
      return status.phase + "...";
  }
}

// the job's per-input reports, by the input's entry id
export function reportsByEntry(status) {
  const byEntry = {};
  for (const report of status.inputs || []) byEntry[report.entry_id] = report;
  return byEntry;
}

// ready edits nobody decided yet: neither confirmed nor rejected
export function pendingKeys(reports) {
  const keys = [];
  for (const report of reports) {
    const decided = new Set([...(report.confirmed || []), ...(report.rejected || [])]);
    for (const edit of report.ready || []) {
      if (!decided.has(edit.key)) keys.push(edit.key);
    }
  }
  return keys;
}

// The inputs (entry ids) whose report is not what the next run reads:
// one whose words changed or that is gone, and every one after it or
// after a decision above the last run did not know that changes the
// sheet they are read on. They are decided once read again. `items`:
// the inputs list, null when not loaded (words are then not compared).
// As the server decides it (`_outdated`).
export function outdatedEntries(items, reports) {
  const texts = items ? new Map(items.map((item) => [item.entry_id, item.text])) : null;
  const ordered = Object.values(reports).sort((a, b) => a.input - b.input);
  const outdated = new Set();
  let stale = false;
  for (const report of ordered) {
    if (texts && report.text !== undefined && texts.get(report.entry_id) !== report.text) stale = true;
    if (stale) outdated.add(report.entry_id);
    if (changesBelow(report)) stale = true;
  }
  return outdated;
}

// a decision the last run did not know that changes the sheet the
// inputs after it were read on; not a confirmation: they were read with it
function changesBelow(report) {
  return ["unconfirmed_since", "rejected_since", "unrejected_since"]
    .some((field) => (report[field] || []).length);
}

// The update decided next, one by one: the first with an undecided
// edit (entry id), null when none. An edit is read on the sheet the
// ones above leave, so it waits for them. As the server decides it
// (`_turn`).
export function turnEntry(reports) {
  const ordered = Object.values(reports).sort((a, b) => a.input - b.input);
  const first = ordered.find((report) => pendingKeys([report]).length);
  return first ? first.entry_id : null;
}

// the pending edits a word check marked: confirm all takes them too,
// its label says how many
export function checkedKeys(reports) {
  const marked = new Set();
  for (const report of reports) {
    for (const edit of report.ready || []) {
      if ((edit.checks || []).length) marked.add(edit.key);
    }
  }
  return pendingKeys(reports).filter((key) => marked.has(key));
}

// an input one of whose edits is confirmed and not held: by the report
// of its words, else by the inputs list (`applied`, from the file)
export function inputApplied(item, report) {
  if (report && (report.text === undefined || report.text === item.text)) {
    return written(report).length > 0;
  }
  return Boolean(item.applied);
}

// The warning before revising or deleting an applied input: the job
// rebuilds the sheet without its edits and reads the inputs after it
// again. Null for an input with nothing written.
export function rewriteWarning(action, items, reports, entryId) {
  const at = items.findIndex((item) => item.entry_id === entryId);
  if (at < 0 || !inputApplied(items[at], reports[entryId])) return null;
  const later = items.slice(at + 1).filter((item) => inputApplied(item, reports[item.entry_id])).length;
  const first = action === "delete"
    ? "This update was written into the sheet. Deleting it takes its edits out of the sheet."
    : "This update was written into the sheet. Changing its words reads it again; its edits need your confirmation again.";
  if (!later) return first;
  return first + " " + (later === 1
    ? "The 1 written update after it is read again: where what it shows changes, it needs your decision again."
    : "The " + later + " written updates after it are read again: where what they show changes, they need your decision again.");
}

// The one-word state of an input, for the chip in its card header:
// what the job made of it, or where the job is with it. Null while it
// has no text (the body says "transcribing...") and when no job is at
// work on this page.
// `outdated`: its report is read again before it is decided.
export function inputState(item, report, jobRunning, canConfirm, outdated = false) {
  if (!item.text) return null;
  const reported = report && (report.text === undefined || report.text === item.text);
  if (!reported) {
    if (item.applied) return { state: "applied", label: "Applied" };
    if (!canConfirm) return null;
    return jobRunning
      ? { state: "reading", label: "Reading..." }
      : { state: "waiting", label: "Waiting for Recompute" };
  }
  if (report.refused) return { state: "refused", label: "Not written" };
  if (outdated) {
    return jobRunning
      ? { state: "reading", label: "Reading..." }
      : { state: "waiting", label: "Waiting for Recompute" };
  }
  const pending = pendingKeys([report]).length;
  if (pending) {
    const label = pending === 1 ? "1 edit to confirm" : pending + " edits to confirm";
    // which new edit a lost decision was is not known (other keys):
    // only how many there were
    const before = report.decide_again || 0;
    if (!before) return { state: "ready", label };
    if (before >= pending) return { state: "ready", label: label + " again" };
    return { state: "ready", label: label + " · " + before + " decided before" };
  }
  if ((report.confirmed_since || []).length) return { state: "writing", label: "Confirmed, writing..." };
  if ((report.held || []).length) return { state: "held", label: "Confirmed, waiting for the updates above" };
  const rejected = (report.rejected || []).length;
  if ((report.confirmed || []).length || rejected) {
    // partly applied: the rest is told under the input, to rephrase
    const applied = (report.confirmed || []).length > 0;
    const parts = [applied ? "Applied" : "Rejected"];
    if (applied && rejected) parts.push(rejected + " rejected");
    if (report.unresolved) parts.push(report.unresolved + " unclear");
    if (report.not_applied) parts.push(report.not_applied + " not applied");
    return { state: applied ? "applied" : "rejected", label: parts.join(" · ") };
  }
  // found, but not settled, refused or already so: the reasons are
  // under the input
  if (report.unresolved) return { state: "unclear", label: "Unclear" };
  if (report.not_applied) return { state: "not-applied", label: "Not applied" };
  if (report.unchanged) return { state: "none", label: "Nothing to change" };
  return { state: "none", label: "No edit found" };
}

// the label of the button that confirms every pending edit
// `checked`: how many of them have a check
export function confirmAllLabel(pending, checked = 0) {
  const label = pending === 1 ? "Confirm the edit" : "Confirm all " + pending + " edits";
  if (!checked) return label;
  if (pending === 1) return label + ", with a check";
  if (checked === pending) return label + ", all with a check";
  return label + ", " + checked + " with a check";
}

// The inputs no job has read in these words: without a report, with a
// report of other words (revised), or a report without an input
// (deleted: with every input deleted, the job writes the original
// back). Empty while an input has no text yet.
export function inputsToRead(items, reports) {
  if (items.some((item) => !item.text)) return [];
  const ids = items
    .filter((item) => !reports[item.entry_id] || reports[item.entry_id].text !== item.text)
    .map((item) => item.entry_id);
  const present = new Set(items.map((item) => item.entry_id));
  for (const id of Object.keys(reports)) if (!present.has(id)) ids.push(id);
  return ids;
}

// what a job is started for: the inputs and their words. The same key
// after a failed job is the same failure, so it is not started again.
export function readKey(items) {
  return JSON.stringify(items.map((item) => [item.entry_id, item.text || ""]));
}

// the preview without the lines the ready edits already show, the words
// each came from (the input's own text is right above), and the coverage
// warning: it means part of the update made no edit, but it fires on the
// step named ("step 1 UV ozone cleaning") more often than on a dropped
// clause, so it is hidden until the check is precise (kit_known_gaps)
export function previewNotes(report) {
  const ready = report.ready || [];
  const shown = new Set(ready.map((edit) => edit.line));
  // the samples already at the value stand in the edit's samples cell
  for (const edit of ready) {
    if (edit.unchanged) shown.add(`   · ${edit.unchanged}, nothing to change`);
  }
  return (report.preview || "").split("\n")
    .filter((line) => line && !shown.has(line) && !/^\s+from "/.test(line)
      && !line.startsWith("⚠ no update in:"));
}

// the line under an update a decision above changed: its earlier
// decisions match none of its edits now
export function resetNote(report) {
  const lost = report.decide_again || 0;
  if (!lost) return null;
  return "An update above changed what this one shows: "
    + (lost === 1 ? "your decision here is" : "your " + lost + " decisions here are")
    + " asked again.";
}

// the confirmed edits of a report the last run wrote: not confirmed
// since, not held for the updates above
function written(report) {
  const not = new Set([...(report.confirmed_since || []), ...(report.held || [])]);
  return (report.confirmed || []).filter((key) => !not.has(key));
}

// the confirmed edits the last run wrote
export function writtenCount(status) {
  let count = 0;
  for (const report of status.inputs || []) {
    if (!report.refused) count += written(report).length;
  }
  return count;
}

// What the sheet holds now, as rows of a table grouped by step: the
// job's own fold of its changelog (status.written: the last value of
// each cell, overlapping edits split by the samples each value is left
// on), in sheet order. A status of an older job has none: then the
// written edits of its reports, grouped by step in the sheet's order
// (steps are numbered "1: Cleaning UV-Ozone"), within a step in the
// order written; a report of an older job has only the line. `first`
// marks the first row of a step.
export function writtenEdits(status) {
  const rows = Array.isArray(status.written)
    ? status.written.map((row) => ({ ...row }))
    : writtenFromReports(status);
  let step = {}; // equal to no step, not even an undefined one
  for (const row of rows) {
    row.first = row.step !== step;
    step = row.step;
  }
  return rows;
}

function writtenFromReports(status) {
  const rows = [];
  for (const report of status.inputs || []) {
    if (report.refused) continue;
    const keys = new Set(written(report));
    for (const edit of report.ready || []) {
      if (!keys.has(edit.key)) continue;
      rows.push(edit.column === undefined
        ? { line: edit.line }
        : { step: edit.step, column: edit.column, value: edit.new, samples: edit.samples });
    }
  }
  // numbered steps by number; the rest after them, by name, so the same
  // name makes one group; ties keep the written order (sort is stable)
  const number = (row) => parseInt(row.step, 10);
  rows.sort((a, b) => {
    const [x, y] = [number(a), number(b)];
    if (Number.isNaN(x) !== Number.isNaN(y)) return Number.isNaN(x) ? 1 : -1;
    if (!Number.isNaN(x)) return x - y;
    return (a.step || "").localeCompare(b.step || "");
  });
  return rows;
}

// edits confirmed, withdrawn (their update revised or deleted),
// rejected or a rejection taken back, since the last run: the final
// sheet, or the sheet the later updates were read on, is not up to
// date with them
function sinceLastRun(report) {
  return [
    ...(report.confirmed_since || []), ...(report.unconfirmed_since || []),
    ...(report.rejected_since || []), ...(report.unrejected_since || []),
  ];
}

// Why the edited sheet is not to download yet, or "": every edit
// decided, every update read in its words, nothing decided since the
// last run. `items`: the inputs list, null when not loaded. As the
// server decides it (`_not_final`).
export function downloadBlock(status, items) {
  const reports = status.inputs || [];
  const unsettled = settledBlock(status, items);
  if (unsettled) return unsettled;
  const pending = pendingKeys(reports).length;
  if (pending) {
    return "Decide every edit first: " + count(pending, "edit waits", "edits wait") + " for a decision";
  }
  return "";
}

// Why the reports are not what the next run reads, or "": every update
// read in its words, nothing decided since the last run. As the server
// decides it (`_not_settled`).
export function settledBlock(status, items) {
  const reports = status.inputs || [];
  if (reports.some((report) => sinceLastRun(report).length)) {
    return "The sheet is being written again: edits were decided since";
  }
  // inputsToRead is empty while a recording is transcribed (no job can
  // start then): that recording is not read either
  if (items && (items.some((item) => !item.text) || inputsToRead(items, reportsByEntry(status)).length)) {
    return "An update is not read yet";
  }
  return "";
}

// The Confirm all review of a completed status, in update order: the
// undecided edits (to confirm, or untick to reject), the rejected ones,
// and the updates with a part that is not an edit (skipped: unclear,
// not applied, not written). `items` give the titles.
export function reviewRows(status, items) {
  const titles = new Map(items.map((item, at) => [item.entry_id, item.label || "Update " + (at + 1)]));
  const rows = { confirm: [], rejected: [], skipped: [] };
  const ordered = [...(status.inputs || [])].sort((a, b) => a.input - b.input);
  for (const report of ordered) {
    const title = titles.get(report.entry_id) || "Update " + report.input;
    const entry = report.entry_id;
    const pending = new Set(pendingKeys([report]));
    const rejected = new Set(report.rejected || []);
    for (const edit of report.ready || []) {
      if (pending.has(edit.key)) rows.confirm.push({ entry, title, key: edit.key, line: edit.line });
      else if (rejected.has(edit.key)) rows.rejected.push({ entry, title, key: edit.key, line: edit.line });
    }
    const why = skippedWhy(report);
    if (why) rows.skipped.push({ entry, title, line: why });
  }
  return rows;
}

function skippedWhy(report) {
  if (report.refused) return "not written: " + report.refused;
  const parts = [];
  if (report.unresolved) parts.push(count(report.unresolved, "part unclear", "parts unclear"));
  if (report.not_applied) parts.push(count(report.not_applied, "part not applied", "parts not applied"));
  return parts.join(" · ");
}

// What each update shows in the review, to mark the ones a rejection
// changed: its undecided edits and what is skipped
export function reviewSnapshot(status) {
  const shown = {};
  for (const report of status.inputs || []) {
    const pending = new Set(pendingKeys([report]));
    shown[report.entry_id] = JSON.stringify([
      (report.ready || []).filter((edit) => pending.has(edit.key)).map((edit) => edit.line),
      skippedWhy(report),
    ]);
  }
  return shown;
}

// the updates (entry ids) shown differently than in `before`
export function changedEntries(before, after) {
  if (!before) return new Set();
  return new Set(Object.keys(after).filter((id) => id in before && before[id] !== after[id]));
}

// the label of the review's button: what it does with the ticks
export function reviewAction(toConfirm, unticked) {
  if (unticked) return "Reject " + unticked + " and check";
  return toConfirm === 1 ? "Confirm 1 edit" : "Confirm " + toConfirm + " edits";
}

// What Undo last asks before taking back the last click
// (status.last_decision), or null when there is none
export function undoQuestion(status) {
  const last = status.last_decision || [];
  if (!last.length) return null;
  if (last.length === 1) {
    const [{ key, decision }] = last;
    const edit = (status.inputs || []).flatMap((report) => report.ready || [])
      .find((e) => e.key === key);
    const verb = decision === "confirmed" ? "confirmation" : "rejection";
    return "Undo your last " + verb + (edit ? ": " + edit.line + "?" : "?")
      + " The edit waits for your decision again.";
  }
  const confirmed = last.filter((d) => d.decision === "confirmed").length;
  const rejected = last.length - confirmed;
  const parts = [];
  if (confirmed) parts.push(count(confirmed, "edit confirmed", "edits confirmed"));
  if (rejected) parts.push(count(rejected, "edit rejected", "edits rejected"));
  return "Undo your last decision: " + parts.join(" and ") + "? They wait for your decision again.";
}

// how many decisions Reset all takes back
export function decisionCount(status) {
  return (status.inputs || []).reduce(
    (n, report) => n + (report.confirmed || []).length + (report.rejected || []).length, 0);
}

// those edits' keys, as one string: "" when the last run wrote everything.
// Each key says what happened to it: rejecting an edit again after
// taking the rejection back is a new decision, not the one written
const SINCE = {
  confirmed_since: "confirmed", unconfirmed_since: "withdrawn",
  rejected_since: "rejected", unrejected_since: "unrejected",
};

export function unwrittenKey(status) {
  return (status.inputs || [])
    .flatMap((report) => Object.entries(SINCE)
      .flatMap(([field, what]) => (report[field] || []).map((key) => what + " " + key)))
    .sort().join("\n");
}

function count(n, one, many) {
  return n === 1 ? "1 " + one : n + " " + many;
}

// the parts of a completed job's summary, in the researcher's terms
function summaryParts(status) {
  const reports = status.inputs || [];
  const parts = [];
  const written = writtenCount(status);
  if (written) parts.push(count(written, "edit written", "edits written"));
  const pending = pendingKeys(reports).length;
  if (pending) parts.push(count(pending, "edit waits", "edits wait") + " for your confirmation");
  const since = reports.reduce((n, report) => n + (report.confirmed_since || []).length, 0);
  if (since) parts.push(count(since, "edit confirmed", "edits confirmed") + ", being written");
  const rejected = reports.reduce((n, report) => n + (report.rejected || []).length, 0);
  if (rejected) parts.push(count(rejected, "edit rejected", "edits rejected"));
  const withdrawn = reports.reduce((n, report) => n + (report.unconfirmed_since || []).length, 0);
  if (withdrawn) parts.push(count(withdrawn, "edit", "edits") + " of a changed update, being taken out");
  const held = reports.reduce((n, report) => n + (report.held || []).length, 0);
  if (held) parts.push(count(held, "edit confirmed waits", "edits confirmed wait") + " for the updates above");
  const undone = reports.reduce((n, report) => n + (report.unrejected_since || []).length, 0);
  if (undone) parts.push(count(undone, "rejection", "rejections") + " taken back, the updates after it are read again");
  const refused = reports.filter((report) => report.refused).length;
  if (refused) parts.push(count(refused, "update could not", "updates could not") + " be written");
  // why decided edits wait again; other stale confirmations
  // (status.stale_confirmations) are not a line
  const again = reports.filter((report) => report.decide_again).length;
  if (again) {
    parts.push(count(again, "update was", "updates were") + " read again after an earlier update changed");
  }
  return parts;
}

// one line for a completed job, e.g.
// "2 edits written · 1 edit waits for your confirmation"
export function editSummary(status) {
  return summaryParts(status).join(" · ") || "Nothing written yet.";
}

// the name the server gave the download (RFC 6266), else the fallback
export function downloadFilename(contentDisposition, fallback) {
  const header = contentDisposition || "";
  const encoded = header.match(/filename\*=UTF-8''([^;]+)/i);
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1]);
    } catch { /* fall through */ }
  }
  const plain = header.match(/filename="([^"]*)"/i);
  return plain ? plain[1] : fallback;
}

// The question before Confirm all (#208): what is confirmed, and that
// every update is locked then, skipped parts too
export function confirmQuestion(status) {
  const reports = status.inputs || [];
  const keys = pendingKeys(reports);
  const updates = reports.filter((report) => pendingKeys([report]).length).length;
  const skipped = reports.filter((report) => skippedWhy(report)).length;
  let question = "Confirm " + count(keys.length, "edit", "edits") + " in "
    + count(updates, "update", "updates") + "? They are written into the sheet, and every update "
    + "is locked: only Reset all changes them.";
  if (skipped) {
    question += " " + (skipped === 1 ? "1 update has" : skipped + " updates have")
      + " a part that is not an edit (unclear, not applied): locked too. Fix it first, or Reset all later.";
  }
  return question;
}

// the unticks of the last round whose edit is gone once read again (its
// key changed with the sheet), by entry id: `sent` from sentUnticks()
export function lostUnticks(sent, status) {
  const ready = new Map((status.inputs || []).map(
    (report) => [report.entry_id, new Set((report.ready || []).map((edit) => edit.key))]));
  const lost = {};
  for (const untick of sent) {
    if (ready.get(untick.entry)?.has(untick.key)) continue;
    (lost[untick.entry] ||= []).push(untick.line);
  }
  return lost;
}

// the Recompute button's label: the round's changes and the updates to read
export function recomputeLabel(changes) {
  return "Recompute (" + count(changes, "change", "changes") + ")";
}
