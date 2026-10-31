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

// the preview without the lines the ready edits already show
export function previewNotes(report) {
  const shown = new Set((report.ready || []).map((edit) => edit.line));
  return (report.preview || "").split("\n").filter((line) => line && !shown.has(line));
}

function count(n, one, many) {
  return n === 1 ? "1 " + one : n + " " + many;
}

// one line for a completed job, e.g.
// "4 cells changed · 2 edits await confirmation · 1 update refused"
export function editSummary(status) {
  const reports = status.inputs || [];
  const parts = [count(status.changes ?? 0, "cell changed", "cells changed")];
  const pending = pendingKeys(reports).length;
  if (pending) parts.push(count(pending, "edit awaits confirmation", "edits await confirmation"));
  const refused = reports.filter((report) => report.refused).length;
  if (refused) parts.push(count(refused, "update refused", "updates refused"));
  if (status.stale_confirmations) {
    parts.push(count(status.stale_confirmations, "confirmation is stale", "confirmations are stale")
      + " (the input changed since): look again");
  }
  return parts.join(" · ");
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
