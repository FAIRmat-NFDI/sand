// What went wrong lately on the page (errors shown, failed requests, JS
// errors), sent with a "Something's wrong" report: the user often only
// saw "it didn't work". No imports, so ui.js can use it.

const MAX_ERRORS = 20;
const MAX_LINE = 1000;

const errors = [];

export function rememberError(text, now = new Date()) {
  const line = now.toISOString().slice(11, 19) + " " + String(text);
  errors.push(line.slice(0, MAX_LINE));
  if (errors.length > MAX_ERRORS) errors.shift();
}

// newest last
export function recentErrors() {
  return [...errors];
}

// "POST api/input-collections/u1/edit-async 502"; the query is dropped:
// it only repeats ids
export function requestLine(method, url, status) {
  const path = String(url).replace(/^https?:\/\/[^/]+/, "").split("?")[0];
  return (method || "GET").toUpperCase() + " " + path + " " + status;
}

// Every fetch on the page goes through here: one place sees all failures.
export function watchPage(win = window) {
  const fetch = win.fetch.bind(win);
  win.fetch = async (input, init) => {
    const method = init?.method || input?.method;
    const url = input?.url || input;
    try {
      const res = await fetch(input, init);
      if (!res.ok) rememberError(requestLine(method, url, res.status));
      return res;
    } catch (err) {
      rememberError(requestLine(method, url, "failed: " + err.message));
      throw err;
    }
  };
  win.addEventListener("error", (event) => rememberError("JS error: " + event.message));
  win.addEventListener("unhandledrejection", (event) => {
    rememberError("JS error: " + (event.reason?.message || event.reason));
  });
}
