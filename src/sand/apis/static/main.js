// Entry point: log in, wire each card, and route the experiment
// selection to the cards that depend on it.

import { initAuth, login, logout } from "./api.js";
import { initExperiments, loadExperiments, onExperimentSelected } from "./experiments.js";
import { initExtract, showExtraction } from "./extract.js";
import { initInputs, showInputs } from "./inputs.js";
import { initLiveToggle } from "./live-transcript.js";
import { initNotes } from "./notes.js";
import { initRecord } from "./record.js";
import { showError } from "./ui.js";

function showLoginPrompt() {
  document.getElementById("login-prompt").style.display = "block";
  document.getElementById("app-content").style.display = "none";
  document.getElementById("auth-area").innerHTML = "";
}

function showApp(userName) {
  document.getElementById("login-prompt").style.display = "none";
  document.getElementById("app-content").style.display = "block";

  const nameEl = document.createElement("span");
  nameEl.textContent = userName;

  const icon = document.createElement("span");
  icon.className = "material-icons";
  icon.textContent = "logout";

  const logoutBtn = document.createElement("button");
  logoutBtn.className = "btn btn-text";
  logoutBtn.id = "logout-btn";
  logoutBtn.appendChild(icon);
  logoutBtn.addEventListener("click", logout);

  document.getElementById("auth-area").replaceChildren(nameEl, logoutBtn);

  loadExperiments().catch((err) => {
    showError("Could not load experiments: " + err.message);
  });
}

initExperiments();
initRecord();
initNotes();
initInputs();
initExtract();
initLiveToggle();

onExperimentSelected((experiment) => {
  showExtraction(experiment);
  showInputs(experiment);
});

document.getElementById("login-btn").addEventListener("click", login);

initAuth({ onLogin: showApp, onLogout: showLoginPrompt }).catch((err) => {
  console.error("Auth init failed:", err);
  showLoginPrompt();
});
