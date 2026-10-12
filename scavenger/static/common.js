// Shared helpers for the player and admin pages.

async function api(method, path, body, headers = {}) {
  const opts = { method, headers: { ...headers } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const err = new Error(data.error || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

// Build DOM nodes without innerHTML so user-supplied text is never parsed as HTML.
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "class") node.className = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : String(c));
  }
  return node;
}

function $(id) { return document.getElementById(id); }

function flash(target, message, kind = "ok") {
  target.replaceChildren(el("div", { class: `flash ${kind}` }, message));
  if (kind === "ok") setTimeout(() => target.replaceChildren(), 2500);
}

function storageGet(key) {
  try { return localStorage.getItem(key); } catch (_) { return null; }
}
function storageSet(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch (_) {}
}

function formatTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : "—";
}
