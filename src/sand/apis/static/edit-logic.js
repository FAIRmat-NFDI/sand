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

// ready edits nobody confirmed yet
export function pendingKeys(reports) {
  const keys = [];
  for (const report of reports) {
    const confirmed = new Set(report.confirmed || []);
    for (const edit of report.ready || []) {
      if (!confirmed.has(edit.key)) keys.push(edit.key);
    }
  }
  return keys;
}

// the pending edits "confirm all" may confirm: not those a word check
// marked, which the user confirms one by one
export function bulkKeys(reports) {
  const marked = new Set();
  for (const report of reports) {
    for (const edit of report.ready || []) {
      if ((edit.checks || []).length) marked.add(edit.key);
    }
  }
  return pendingKeys(reports).filter((key) => !marked.has(key));
}

// an input one of whose edits was just confirmed: its words are a
// record (the inputs list says so too, as `locked`, from the file)
export function inputLocked(report) {
  return Boolean(report && (report.confirmed || []).length);
}

// The one-word state of an input, for the chip in its card header:
// what the job made of it, or where the job is with it. Null while it
// has no text (the body says "transcribing...") and when no job is at
// work on this page.
export function inputState(item, report, jobRunning, canConfirm) {
  if (!item.text) return null;
  const reported = report && (report.text === undefined || report.text === item.text);
  if (!reported) {
    if (item.locked) return { state: "applied", label: "Applied" };
    if (!canConfirm) return null;
    return jobRunning
      ? { state: "reading", label: "Reading..." }
      : { state: "waiting", label: "Waiting to be read" };
  }
  if (report.refused) return { state: "refused", label: "Not written" };
  const pending = pendingKeys([report]).length;
  if (pending) {
    return { state: "ready", label: pending === 1 ? "1 edit to confirm" : pending + " edits to confirm" };
  }
  if ((report.confirmed_since || []).length) return { state: "writing", label: "Confirmed, writing..." };
  if ((report.confirmed || []).length) return { state: "applied", label: "Applied" };
  return { state: "none", label: "No edit found" };
}

// the label of the button that confirms every pending edit
// `checked`: pending edits with a check, which it leaves out
export function confirmAllLabel(pending, checked = 0) {
  if (checked) return "Confirm " + pending + (pending === 1 ? " edit" : " edits") + " without a check";
  if (pending === 1) return "Confirm the edit";
  return "Confirm all " + pending + " edits";
}

// The inputs no job has read in these words: without a report, with a
// report of other words (revised), or a report without an input
// (deleted). Empty while an input has no text yet, and with no inputs
// at all: the job refuses to start then.
export function inputsToRead(items, reports) {
  if (!items.length || items.some((item) => !item.text)) return [];
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

// the confirmed edits the last run wrote
export function writtenCount(status) {
  let count = 0;
  for (const report of status.inputs || []) {
    if (report.refused) continue;
    const since = new Set(report.confirmed_since || []);
    count += (report.confirmed || []).filter((key) => !since.has(key)).length;
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
    const confirmed = new Set(report.confirmed || []);
    const since = new Set(report.confirmed_since || []);
    for (const edit of report.ready || []) {
      if (!confirmed.has(edit.key) || since.has(edit.key)) continue;
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

// confirmed edits the last run did not write: the final sheet lacks them
export function unwrittenCount(status) {
  return (status.inputs || []).reduce((n, report) => n + (report.confirmed_since || []).length, 0);
}

// those edits' keys, as one string: "" when the last run wrote everything
export function unwrittenKey(status) {
  return (status.inputs || []).flatMap((report) => report.confirmed_since || []).sort().join("\n");
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
  const since = unwrittenCount(status);
  if (since) parts.push(count(since, "edit confirmed", "edits confirmed") + ", being written");
  const refused = reports.filter((report) => report.refused).length;
  if (refused) parts.push(count(refused, "update could not", "updates could not") + " be written");
  // stale confirmations (status.stale_confirmations) are not a line: an
  // edit that lost its confirmation shows as waiting again
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
