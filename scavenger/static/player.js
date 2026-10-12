const TOKEN_KEY = "scavenger.playerToken";
const CACHE_KEY = "scavenger.lastState";

let token = storageGet(TOKEN_KEY);
let view = null;      // last game state from the server (or the offline cache)
let board = [];       // last leaderboard
let queued = [];      // outbox entries for this player, waiting to be sent
let offline = false;
let lastSignature = "";
let pollTimer = null;
let syncing = false;
const itemMessages = new Map(); // item id -> {text, kind, at}; errors stay until the next action, others fade
const MESSAGE_MS = 6000;
const drafts = new Map();       // item id -> typed answer, kept across re-renders
const queuedUrls = new Map();   // outbox id -> object URL of a queued photo

const auth = () => ({ "X-Player-Token": token });

if ("serviceWorker" in navigator) {
  // Only works on HTTPS or localhost; elsewhere the page still works but can't reload offline.
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

// ----- local copies ----------------------------------------------------------

function saveCache() {
  storageSet(CACHE_KEY, JSON.stringify({ token, view, board }));
}

function loadCache() {
  try {
    const cached = JSON.parse(storageGet(CACHE_KEY) || "null");
    if (cached && cached.token === token) {
      view = cached.view;
      board = cached.board || [];
    }
  } catch (_) {}
}

async function loadQueued() {
  const all = await outbox.all();
  queued = all.filter((e) => e.token === token);
}

function setOffline(value) {
  offline = value;
  renderBanner();
}

// ----- screens ---------------------------------------------------------------

async function showJoin() {
  token = null;
  view = null;
  storageSet(TOKEN_KEY, null);
  storageSet(CACHE_KEY, null);
  await outbox.clear();
  queued = [];
  clearInterval(pollTimer);
  $("game-view").classList.add("hidden");
  $("join-view").classList.remove("hidden");
}

async function startGame() {
  loadCache();
  await loadQueued();
  if (view) showGame();
  await refresh();
  if (!token) return;
  if (!view) {
    flash($("join-msg"), "Can't reach the server — check your connection.", "err");
    return;
  }
  showGame();
  syncOutbox();
  clearInterval(pollTimer);
  pollTimer = setInterval(() => { syncOutbox(); refresh(); }, 15000);
}

function showGame() {
  $("join-view").classList.add("hidden");
  $("game-view").classList.remove("hidden");
  render(true);
}

// Fetch the latest state; on a network failure keep showing the cached one.
async function refresh() {
  if (!token) return;
  try {
    view = await api("GET", "/api/play", undefined, auth());
    const lb = await api("GET", `/api/leaderboard?code=${encodeURIComponent(view.game.code)}`);
    board = lb.players;
    setOffline(false);
    saveCache();
  } catch (e) {
    if (e.network) setOffline(true);
    else if (e.status === 401) return showJoin();
  }
  if (view) render();
}

// ----- submitting --------------------------------------------------------------

async function submit(entry) {
  if (navigator.onLine === false) return enqueue(entry);
  try {
    const res = await send(entry);
    setOffline(false);
    applyResult(entry, res);
  } catch (e) {
    if (e.network) return enqueue(entry);
    if (e.status === 401) return showJoin();
    itemMessages.set(entry.item_id, { text: e.message, kind: "err", at: Date.now() });
    render(true);
  }
}

function send(entry) {
  if (entry.kind === "answer") {
    return api("POST", "/api/play/answer",
      { item_id: entry.item_id, answer: entry.answer, captured_at: entry.captured_at }, auth());
  }
  const qs = `item_id=${entry.item_id}&captured_at=${entry.captured_at}`;
  return api("POST", `/api/play/photo?${qs}`, entry.blob, auth());
}

function applyResult(entry, res, fromQueue = false) {
  const { correct, photo_id, status, ...fresh } = res;
  view = fresh;
  saveCache();
  if (entry.kind === "answer") {
    if (correct) {
      drafts.delete(entry.item_id);
      itemMessages.set(entry.item_id, { text: "Correct!", kind: "ok", at: Date.now() });
    } else {
      itemMessages.set(entry.item_id, {
        text: fromQueue ? `Your saved answer “${entry.answer}” wasn't right — try again.` : "Not quite — try again!",
        kind: "err",
      });
    }
  } else {
    itemMessages.set(entry.item_id, status === "approved"
      ? { text: "Photo accepted!", kind: "ok", at: Date.now() }
      : { text: "Photo sent — waiting for the organizer to check it.", kind: "ok", at: Date.now() });
  }
  render(true);
  refresh();
}

async function enqueue(entry) {
  if (entry.kind === "photo") {
    // A newer photo replaces an older one still waiting to be sent.
    for (const old of queued.filter((e) => e.kind === "photo" && e.item_id === entry.item_id)) {
      await outbox.remove(old.id);
    }
  }
  await outbox.add(entry);
  await loadQueued();
  setOffline(true);
  itemMessages.delete(entry.item_id);
  if (entry.kind === "answer") drafts.delete(entry.item_id);
  render(true);
}

// Send queued answers and photos, oldest first. Stops at the first network failure.
async function syncOutbox() {
  if (syncing || !token) return;
  syncing = true;
  try {
    await loadQueued();
    for (const entry of queued) {
      try {
        const res = await send(entry);
        await outbox.remove(entry.id);
        setOffline(false);
        applyResult(entry, res, true);
      } catch (e) {
        if (e.network) { setOffline(true); break; }
        if (e.status === 401) { await showJoin(); return; }
        if (e.status === 403) {
          // Game paused or ended: keep it, in case the organizer re-opens the game.
          entry.error = e.message;
          await outbox.put(entry);
          break;
        }
        // Anything else won't succeed on retry (item removed, already found, bad photo).
        await outbox.remove(entry.id);
        if (e.status !== 409) itemMessages.set(entry.item_id, { text: `Couldn't send your saved entry: ${e.message}`, kind: "err", at: Date.now() });
      }
    }
  } finally {
    syncing = false;
    await loadQueued();
    if (view) render(); // only redraws items if the queue changed, so typing isn't interrupted
  }
}

// Phone photos are several MB; shrink to a JPEG that uploads quickly and fits offline storage.
async function shrinkPhoto(file, maxSide = 1600, quality = 0.8) {
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise((resolve, reject) => {
      const i = new Image();
      i.onload = () => resolve(i);
      i.onerror = () => reject(new Error("Couldn't read that photo. Please take it again."));
      i.src = url;
    });
    const scale = Math.min(1, maxSide / Math.max(img.naturalWidth, img.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(img.naturalWidth * scale);
    canvas.height = Math.round(img.naturalHeight * scale);
    canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
    if (!blob) throw new Error("Couldn't process that photo. Please try again.");
    return blob;
  } finally {
    URL.revokeObjectURL(url);
  }
}

async function handlePhoto(item, file) {
  if (!file) return;
  const capturedAt = Date.now() / 1000;
  itemMessages.set(item.id, { text: "Preparing photo…", kind: "ok", at: Date.now() });
  render(true);
  try {
    const blob = await shrinkPhoto(file);
    await submit({ token, kind: "photo", item_id: item.id, title: item.title, blob, captured_at: capturedAt });
  } catch (e) {
    itemMessages.set(item.id, { text: e.message, kind: "err", at: Date.now() });
    render(true);
  }
}

// ----- rendering -----------------------------------------------------------------

function renderBanner() {
  const waiting = queued.length;
  const banner = $("banner");
  if (!offline && !waiting) return banner.classList.add("hidden");
  banner.classList.remove("hidden");
  const parts = [];
  if (offline) parts.push("You're offline. Keep hunting — answers and photos are saved on this phone.");
  if (waiting) {
    const blocked = queued.find((e) => e.error);
    parts.push(blocked
      ? `${waiting} saved ${waiting === 1 ? "entry" : "entries"} can't be sent: ${blocked.error}`
      : `${waiting} saved ${waiting === 1 ? "entry" : "entries"} will be sent automatically when you're back online.`);
  }
  banner.textContent = parts.join(" ");
}

function renderSummary() {
  const { game, player, items, score, found } = view;
  $("game-name").textContent = game.name;
  $("player-name").textContent = player.name;
  $("game-status").textContent = game.status;
  $("game-status").className = `badge ${game.status}`;
  $("game-desc").textContent = game.description;
  $("score").textContent = score;
  $("found-count").textContent = found;
  $("item-count").textContent = items.length;
  $("progress-bar").style.width = items.length ? `${(100 * found) / items.length}%` : "0%";
  $("ended-msg").classList.toggle("hidden", game.status !== "ended");
}

// Re-render items only when something changed, so a half-typed answer keeps focus.
function render(force = false) {
  renderBanner();
  renderSummary();
  renderLeaderboard();
  const signature = JSON.stringify([
    view.game.status,
    view.items.map((i) => [i.id, i.found, i.photo && i.photo.id, i.photo && i.photo.status]),
    queued.map((e) => e.id),
  ]);
  if (!force && signature === lastSignature) return;
  lastSignature = signature;
  const cards = view.items.map(renderItem);
  if (!cards.length) cards.push(el("p", { class: "muted" }, "No items have been added to this hunt yet."));
  $("items").replaceChildren(...cards);
}

function queuedPhotoUrl(entry) {
  if (!queuedUrls.has(entry.id)) queuedUrls.set(entry.id, URL.createObjectURL(entry.blob));
  return queuedUrls.get(entry.id);
}

function renderItem(item, idx) {
  const open = view.game.status === "active";
  const waiting = queued.filter((e) => e.item_id === item.id);
  const message = itemMessages.get(item.id);
  const parts = [];

  if (item.found) {
    parts.push(el("p", { class: "muted small", style: "margin-top:8px" }, `Found ${formatTime(item.found_at)}`));
    if (item.kind === "photo" && item.photo) {
      parts.push(lazyImage(`/api/play/photos/${item.photo.id}`, auth(), `Your photo of ${item.title}`));
    }
  } else if (item.kind === "photo") {
    const queuedPhoto = waiting.find((e) => e.kind === "photo");
    if (queuedPhoto) {
      parts.push(el("img", { class: "thumb", src: queuedPhotoUrl(queuedPhoto), alt: `Saved photo of ${item.title}` }),
        el("p", { class: "status pending" }, "Saved on this phone — it will upload when you're back online."));
    } else if (item.photo && item.photo.status === "pending") {
      parts.push(lazyImage(`/api/play/photos/${item.photo.id}`, auth(), `Your photo of ${item.title}`),
        el("p", { class: "status pending" }, "Waiting for the organizer to check your photo."));
    } else if (item.photo && item.photo.status === "rejected") {
      parts.push(el("p", { class: "status rejected" }, "The organizer didn't accept your photo. Try another one."));
    }
    if (open) {
      const label = queuedPhoto || (item.photo && item.photo.status === "pending") ? "Retake photo" : "Take photo";
      parts.push(el("div", { style: "margin-top:10px" },
        el("label", { class: "file-button" },
          el("span", { class: "button-look" }, `📷 ${label}`),
          el("input", {
            type: "file", accept: "image/*", capture: "environment", "aria-label": `${label} for ${item.title}`,
            onchange: (ev) => handlePhoto(item, ev.target.files[0]),
          }))));
    }
  } else {
    for (const e of waiting) {
      parts.push(el("p", { class: "status pending" },
        `Your answer “${e.answer}” is saved and will be checked when you're back online.`));
    }
    if (open) {
      const input = el("input", {
        placeholder: "Your answer", maxlength: 500, "aria-label": `Answer for ${item.title}`,
        value: drafts.get(item.id) || "",
        oninput: () => drafts.set(item.id, input.value),
      });
      parts.push(el("form", {
        class: "row", style: "margin-top:10px",
        onsubmit: (ev) => {
          ev.preventDefault();
          if (!input.value.trim()) return;
          submit({ token, kind: "answer", item_id: item.id, title: item.title,
                   answer: input.value.trim(), captured_at: Date.now() / 1000 });
        },
      }, el("div", { class: "grow" }, input), el("button", { type: "submit" }, "Submit")));
    }
  }
  if (message) {
    const node = el("div", { class: `flash ${message.kind}` }, message.text);
    if (message.kind === "ok") {
      setTimeout(() => {
        if (itemMessages.get(item.id) === message) itemMessages.delete(item.id);
        node.remove();
      }, Math.max(0, message.at + MESSAGE_MS - Date.now()));
    }
    parts.push(node);
  }

  return el("div", { class: `card item${item.found ? " found" : ""}` },
    el("div", { class: "row spread" },
      el("h3", {}, `${idx + 1}. ${item.title}`),
      el("span", { class: "muted small" },
        `${item.kind === "photo" ? "📷 " : ""}${item.points} pt${item.points === 1 ? "" : "s"}`)),
    item.clue ? el("p", { style: "margin-top:8px; white-space:pre-wrap" }, item.clue) : null,
    ...parts);
}

function renderLeaderboard() {
  const me = view.player.name.toLowerCase();
  const rows = board.map((p, i) =>
    el("tr", { style: p.name.toLowerCase() === me ? "font-weight:600" : null },
      el("td", {}, i + 1), el("td", {}, p.name), el("td", { class: "num" }, p.found), el("td", { class: "num" }, p.score)));
  $("leaderboard").replaceChildren(
    el("tr", {}, el("th", {}, "#"), el("th", {}, "Player"), el("th", { class: "num" }, "Found"), el("th", { class: "num" }, "Score")),
    ...rows);
  $("board-note").textContent = offline ? "Leaderboard as of your last connection." : "";
}

// ----- events ----------------------------------------------------------------------

$("join-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const res = await api("POST", "/api/join", { code: $("join-code").value, name: $("join-name").value });
    token = res.token;
    storageSet(TOKEN_KEY, token);
    await startGame();
  } catch (e) {
    flash($("join-msg"), e.message, "err");
  }
});

$("leave-btn").addEventListener("click", () => {
  const warning = queued.length
    ? `You have ${queued.length} saved ${queued.length === 1 ? "entry" : "entries"} that haven't been sent yet and will be lost. Leave anyway?`
    : "Leave this game? You'll need to join again with a new name to keep playing.";
  if (confirm(warning)) showJoin();
});
$("refresh-btn").addEventListener("click", () => { syncOutbox(); refresh(); });
window.addEventListener("online", () => { syncOutbox(); refresh(); });
window.addEventListener("offline", () => setOffline(true));

const codeFromUrl = new URLSearchParams(location.search).get("code");
if (codeFromUrl) $("join-code").value = codeFromUrl.toUpperCase();
if (token) startGame();
