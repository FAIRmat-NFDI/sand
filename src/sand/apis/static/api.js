// NOMAD login, and the watch on it.
//
// sand has no login of its own: it uses the NOMAD GUI's. The GUI writes
// its token to the Authorization cookie and rewrites it on every renewal;
// the browser sends the cookie with each request. The token expires, so
// sand works only while a NOMAD GUI is open in another tab of this browser.

const SESSION_CHECK_MS = 5000;
// The GUI renews a minute before the token expires: less than this left
// means no GUI is renewing (closed, or its own login ended).
const EXPIRING_S = 30;

let nomadGuiUrl = "";

// Read from the token, not from the cookie's presence: the cookie
// outlives its token (the GUI sets no usable expiry on it).
function sessionSecondsLeft() {
  const match = document.cookie.match(/(?:^|; )Authorization=([^;]*)/);
  if (!match) return 0;
  try {
    const token = decodeURIComponent(match[1]).replace(/^Bearer /, "");
    const payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const exp = JSON.parse(atob(payload)).exp;
    return Number.isFinite(exp) ? exp - Date.now() / 1000 : 0;
  } catch {
    return 0;
  }
}

// "ok", "expiring" (nobody renews the token) or "lost" (it has expired)
export function sessionState() {
  const left = sessionSecondsLeft();
  if (left <= 0) return "lost";
  return left < EXPIRING_S ? "expiring" : "ok";
}

// null: not logged in. Throws when NOMAD fails, which is not a logout.
async function loggedInName() {
  const res = await fetch("api/me");
  if (res.status === 401) return null;
  if (!res.ok) throw new Error(await errorDetail(res));
  return (await res.json()).name;
}

// onLogout(problem) gets a message when NOMAD failed instead of answering.
// onSession(state) fires on every change of sessionState() after login.
export async function initAuth({ onLogin, onLogout, onSession }) {
  nomadGuiUrl = (await (await fetch("ui-config")).json()).nomad_gui_url;

  let name = null;
  try {
    name = await loggedInName();
    if (name === null) onLogout("");
  } catch (err) {
    onLogout("Could not check your NOMAD login: " + err.message);
  }
  if (name !== null) onLogin(name);

  let state = "ok";
  let checking = false;
  const check = async () => {
    const next = sessionState();
    if (name === null) {
      // logged in to NOMAD meanwhile (e.g. in the tab login() opened)
      if (next === "lost" || checking) return;
      checking = true;
      try {
        name = await loggedInName();
      } catch { /* NOMAD still failing: try again next time */ }
      checking = false;
      if (name !== null) onLogin(name);
      return;
    }
    if (next !== state) {
      state = next;
      onSession(state);
    }
  };
  setInterval(check, SESSION_CHECK_MS);
  // Browsers slow the timer down in a background tab: check at once when
  // the user comes back, typically from logging in to NOMAD.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") check();
  });
  window.addEventListener("focus", check);
}

// Opening the GUI is enough when NOMAD's login is still alive: the GUI
// signs in silently and writes a fresh cookie.
export function login() {
  window.open(nomadGuiUrl, "_blank");
}

export async function errorDetail(res) {
  const body = await res.json().catch(() => null);
  if (!body || body.detail == null) return res.statusText;
  return typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
}

// An experiment endpoint, e.g. experimentUrl(experiment, "notes"). The
// entry id pins the collection: an upload can hold more than one.
export function experimentUrl(experiment, path) {
  return "api/input-collections/" + experiment.upload_id + "/" + path
    + "?collection_entry_id=" + encodeURIComponent(experiment.entry_id);
}
