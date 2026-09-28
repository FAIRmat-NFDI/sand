// Page-wide feedback: the error banner, "saved, view on NOMAD" links and
// the confirm dialog.

const error = document.getElementById("error");

export function showError(msg) {
  error.textContent = msg;
  error.style.display = "block";
}

export function clearError() {
  error.textContent = "";
  error.style.display = "none";
}

export function showEntryLink(el, message, entryUrl, linkText) {
  const link = document.createElement("a");
  link.href = entryUrl;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = linkText;
  el.replaceChildren(document.createTextNode(message + " "), link);
  el.style.display = "block";
}

export function clearEntryLink(el) {
  el.replaceChildren();
  el.style.display = "none";
}

const confirmEl = document.getElementById("confirm-dialog");

// Not window.confirm: NOMAD's iframe blocks browser dialogs (its sandbox
// has no allow-modals), where confirm() answers false without asking.
export function confirmDialog(message, okText) {
  document.getElementById("confirm-message").textContent = message;
  document.getElementById("confirm-ok").textContent = okText;
  confirmEl.showModal();
  return new Promise((resolve) => {
    answer = resolve;
  });
}

let answer = () => {};

// The buttons answer themselves: the close event is held back while the
// tab is hidden. It only serves Escape, and is void after a button.
function closeConfirm(confirmed) {
  answer(confirmed);
  confirmEl.close();
}

document.getElementById("confirm-ok").addEventListener("click", () => closeConfirm(true));
document.getElementById("confirm-cancel").addEventListener("click", () => closeConfirm(false));
confirmEl.addEventListener("close", () => answer(false));
