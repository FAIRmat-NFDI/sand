// NOMAD login (Keycloak) and the authenticated fetch every request uses.

let keycloak = null;

export async function initAuth({ onLogin, onLogout }) {
  const res = await fetch("auth/config");
  const cfg = await res.json();

  keycloak = new Keycloak({
    url: cfg.keycloak_url,
    realm: cfg.keycloak_realm,
    clientId: cfg.keycloak_client_id,
  });

  const authenticated = await keycloak.init({
    onLoad: "check-sso",
    checkLoginIframe: false,
  });

  if (authenticated) {
    onLogin(keycloak.tokenParsed.preferred_username || keycloak.tokenParsed.name || "");
  } else {
    onLogout();
  }

  setInterval(() => {
    if (keycloak.authenticated) {
      keycloak.updateToken(30).catch(() => {
        onLogout();
      });
    }
  }, 10000);
}

export function login() {
  keycloak.login({ redirectUri: window.location.href });
}

export function logout() {
  keycloak.logout({ redirectUri: window.location.href });
}

export function authToken() {
  return keycloak.token;
}

export async function authFetch(url, options = {}) {
  if (keycloak.authenticated) {
    try { await keycloak.updateToken(5); } catch { /* ignore */ }
    options.headers = options.headers || {};
    options.headers["Authorization"] = "Bearer " + keycloak.token;
  }
  return fetch(url, options);
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
