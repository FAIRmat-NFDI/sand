// Page-wide feedback: the error banner and "saved, view on NOMAD" links.

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
