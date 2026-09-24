// The Note card: save a typed step note to the selected experiment.

import { authFetch, experimentUrl } from "./api.js";
import { requireExperiment } from "./experiments.js";
import { reportNewInput } from "./inputs.js";
import { clearEntryLink, clearError, showError } from "./ui.js";

const textArea = document.getElementById("text");
const saveNoteBtn = document.getElementById("save-note-btn");
const noteEntryEl = document.getElementById("note-entry");

async function saveNote() {
  clearError();
  const experiment = requireExperiment();
  if (!experiment) return;
  const text = textArea.value.trim();
  if (!text) {
    showError("Nothing to save. Type a step note first.");
    return;
  }
  saveNoteBtn.disabled = true;
  clearEntryLink(noteEntryEl);
  try {
    const saved = await reportNewInput(
      noteEntryEl,
      authFetch(experimentUrl(experiment, "notes"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      }),
      "Saving the note failed",
      "Note added to " + experiment.name + ".",
      "View note on NOMAD"
    );
    if (saved) textArea.value = "";
  } catch (err) {
    showError("Network error: " + err.message);
  } finally {
    saveNoteBtn.disabled = false;
  }
}

export function initNotes() {
  saveNoteBtn.addEventListener("click", saveNote);
}
