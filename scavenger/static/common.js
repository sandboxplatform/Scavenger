// Shared helpers for the player and admin pages.

// Errors carry `status` (HTTP code) or `network: true` when the server couldn't be reached.
async function request(method, path, body, headers = {}) {
  const opts = { method, headers: { ...headers } };
  if (body instanceof Blob) {
    opts.headers["Content-Type"] = body.type || "application/octet-stream";
    opts.body = body;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  try {
    return await fetch(path, opts);
  } catch (_) {
    const err = new Error("Can't reach the server — check your connection.");
    err.network = true;
    throw err;
  }
}

async function api(method, path, body, headers = {}) {
  const res = await request(method, path, body, headers);
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

// Load an authenticated image (headers can't go on <img src>) as an object URL, cached per path.
const blobUrls = new Map();
async function imageUrl(path, headers) {
  if (!blobUrls.has(path)) {
    const res = await request("GET", path, undefined, headers);
    if (!res.ok) throw new Error(`Couldn't load photo (${res.status})`);
    blobUrls.set(path, URL.createObjectURL(await res.blob()));
  }
  return blobUrls.get(path);
}

function lazyImage(path, headers, alt) {
  const img = el("img", { class: "thumb", alt });
  if (blobUrls.has(path)) {
    img.src = blobUrls.get(path); // avoid a flash when lists re-render
    return img;
  }
  imageUrl(path, headers).then(url => { img.src = url; }).catch(() => { img.alt = "Photo unavailable"; });
  return img;
}
