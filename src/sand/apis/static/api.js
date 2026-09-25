// NOMAD login and the authenticated fetch every request uses.
//
// Embedded in the NOMAD GUI (an iframe on its Dashboards page), sand uses
// NOMAD's session: the GUI keeps its Authorization cookie fresh and the
// browser sends it with every request. In a tab the GUI may be closed and
// its cookie expire, so sand logs in with Keycloak itself (as before) and
// sends the token as a header.

const embedded = window.parent !== window;
let keycloak = null;

export async function initAuth({ onLogin, onLogout }) {
  if (embedded) {
    const res = await fetch("api/me");
    if (!res.ok) {
      onLogout();
      return;
    }
    // NOMAD manages the session: no logout here
    onLogin((await res.json()).name, { canLogout: false });
    return;
  }

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
    onLogin(keycloak.tokenParsed.preferred_username || keycloak.tokenParsed.name || "",
      { canLogout: true });
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
  // Keycloak's login page cannot be shown inside NOMAD's iframe
  if (embedded) {
    window.open(window.location.href, "_blank");
    return;
  }
  keycloak.login({ redirectUri: window.location.href });
}

export function logout() {
  keycloak.logout({ redirectUri: window.location.href });
}

// "" when embedded: the live-transcript socket then authenticates with the
// cookie sent along with its handshake
export function authToken() {
  return keycloak ? keycloak.token : "";
}

export async function authFetch(url, options = {}) {
  if (keycloak?.authenticated) {
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
