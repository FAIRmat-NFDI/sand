// Edit mode's pure logic: what the Apply card and the input tiles show
// for a job status. No DOM, so it runs under node's test runner.

export function describeEditPhase(status) {
  switch (status.phase) {
    case "starting":
      return "Starting...";
    case "collecting":
      return "Collecting the updates...";
    case "interpreting": {
      const total = status.inputs_total ?? 0;
      const current = Math.min((status.inputs_done ?? 0) + 1, total || 1);
      return "Interpreting update " + current + " of " + (total || "?") + "...";
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

// an input one of whose edits was just confirmed: its words are a
// record (the inputs list says so too, as `locked`, from the file)
export function inputLocked(report) {
  return Boolean(report && (report.confirmed || []).length);
}

// the label of the button that confirms every pending edit and writes
export function confirmAllLabel(pending) {
  if (pending === 1) return "Confirm the edit and write";
  return "Confirm all " + pending + " edits and write";
}

// the preview without the lines the ready edits already show, the words
// each came from (the input's own text is right above), and the clauses
// holding no update (nothing to act on)
export function previewNotes(report) {
  const shown = new Set((report.ready || []).map((edit) => edit.line));
  return (report.preview || "").split("\n")
    .filter((line) => line && !shown.has(line) && !/^\s+from "/.test(line)
      && !line.startsWith("⚠ no update in:"));
}

// the edits the last run wrote, one line each in the researcher's words:
// "Time 1 [s] → 100, all 42 samples (1: Cleaning UV-Ozone)"
export function writtenLines(status) {
  const lines = [];
  for (const report of status.inputs || []) {
    if (report.refused) continue;
    const confirmed = new Set(report.confirmed || []);
    const since = new Set(report.confirmed_since || []);
    for (const edit of report.ready || []) {
      if (!confirmed.has(edit.key) || since.has(edit.key)) continue;
      lines.push(edit.column === undefined
        ? edit.line
        : edit.column + " → " + edit.new + ", " + edit.samples + " (" + edit.step + ")");
    }
  }
  return lines;
}

// confirmed edits the last run did not write: the final sheet lacks them
export function unwrittenCount(status) {
  return (status.inputs || []).reduce((n, report) => n + (report.confirmed_since || []).length, 0);
}

function count(n, one, many) {
  return n === 1 ? "1 " + one : n + " " + many;
}

// one line for a completed job, in the researcher's terms, e.g.
// "2 edits written · 1 edit waits for your confirmation"
export function editSummary(status) {
  const reports = status.inputs || [];
  const parts = [];
  const written = writtenLines(status).length;
  if (written) parts.push(count(written, "edit written", "edits written"));
  const pending = pendingKeys(reports).length;
  if (pending) parts.push(count(pending, "edit waits", "edits wait") + " for your confirmation");
  const since = unwrittenCount(status);
  if (since) parts.push(count(since, "edit confirmed", "edits confirmed") + ", not written yet: apply the updates");
  const refused = reports.filter((report) => report.refused).length;
  if (refused) parts.push(count(refused, "update could not", "updates could not") + " be written");
  // stale confirmations (status.stale_confirmations) are not a line: an
  // edit that lost its confirmation shows as waiting again
  return parts.join(" · ") || "Nothing written yet.";
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
