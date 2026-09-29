// Entry point: log in, wire each card, and route the experiment
// selection to the cards that depend on it.

import { initAuth, login } from "./api.js";
import { initExperiments, loadExperiments, onExperimentSelected } from "./experiments.js";
import { initExtract, showExtraction } from "./extract.js";
import { initInputs, showInputs } from "./inputs.js";
import { initLiveToggle } from "./live-transcript.js";
import { initNotes } from "./notes.js";
import { initRecord } from "./record.js";
import { showError } from "./ui.js";
import { initVoiceMode } from "./voice/voice-mode.js";

function showLoginPrompt(problem = "") {
  const problemEl = document.getElementById("login-problem");
  problemEl.textContent = problem;
  problemEl.style.display = problem ? "block" : "none";
  document.getElementById("login-prompt").style.display = "block";
  document.getElementById("app-content").style.display = "none";
  document.getElementById("auth-area").innerHTML = "";
}

// NOMAD manages the login: no logout here
function showApp(userName) {
  document.getElementById("login-prompt").style.display = "none";
  document.getElementById("app-content").style.display = "block";

  const nameEl = document.createElement("span");
  nameEl.textContent = userName;
  document.getElementById("auth-area").replaceChildren(nameEl);

  loadExperiments().catch((err) => {
    showError("Could not load experiments: " + err.message);
  });
}

const SESSION_WARNINGS = {
  expiring: "Your NOMAD login is about to end: NOMAD is not renewing it. "
    + "Keep NOMAD open in this browser while you use SAND.",
  lost: "Your NOMAD login has ended: nothing can be saved. "
    + "Log in to NOMAD again before you stop a recording or save a note.",
};

// The app stays visible: a recording may be running, a note half typed.
function showSession(state) {
  const el = document.getElementById("session-warning");
  if (state === "ok") {
    el.style.display = "none";
    return;
  }
  const link = document.createElement("a");
  link.href = "#";
  link.textContent = "Open NOMAD";
  link.addEventListener("click", (event) => {
    event.preventDefault();
    login();
  });
  el.replaceChildren(document.createTextNode(SESSION_WARNINGS[state] + " "), link);
  el.style.display = "block";
}

initExperiments();
initRecord();
initNotes();
initInputs();
initExtract();
initLiveToggle();
initVoiceMode();

onExperimentSelected((experiment) => {
  showExtraction(experiment);
  showInputs(experiment);
});

document.getElementById("login-btn").addEventListener("click", login);

initAuth({ onLogin: showApp, onLogout: showLoginPrompt, onSession: showSession }).catch((err) => {
  console.error("Auth init failed:", err);
  showLoginPrompt("Could not reach SAND: " + err.message);
});
