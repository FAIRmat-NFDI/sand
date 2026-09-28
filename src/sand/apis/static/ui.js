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
  confirmEl.returnValue = "";
  confirmEl.showModal();
  // Escape closes without a returnValue: a cancel
  return new Promise((resolve) => {
    confirmEl.addEventListener("close", () => resolve(confirmEl.returnValue === "ok"),
      { once: true });
  });
}

document.getElementById("confirm-ok").addEventListener("click", () => confirmEl.close("ok"));
document.getElementById("confirm-cancel").addEventListener("click", () => confirmEl.close());
