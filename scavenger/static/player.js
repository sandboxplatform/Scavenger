const TOKEN_KEY = "scavenger.playerToken";
let token = storageGet(TOKEN_KEY);
let state = null;
let pollTimer = null;

const auth = () => ({ "X-Player-Token": token });

function showJoin() {
  token = null;
  storageSet(TOKEN_KEY, null);
  clearInterval(pollTimer);
  $("game-view").classList.add("hidden");
  $("join-view").classList.remove("hidden");
}

async function loadGame() {
  try {
    state = await api("GET", "/api/play", undefined, auth());
  } catch (e) {
    if (e.status === 401) return showJoin();
    return flash($("game-msg"), e.message, "err");
  }
  $("join-view").classList.add("hidden");
  $("game-view").classList.remove("hidden");
  renderGame();
  loadLeaderboard();
  clearInterval(pollTimer);
  pollTimer = setInterval(() => { loadLeaderboard(); refreshStatus(); }, 15000);
}

// Pick up status changes made by the admin (e.g. game ended) without clobbering typed answers.
async function refreshStatus() {
  try {
    const fresh = await api("GET", "/api/play", undefined, auth());
    const changed = fresh.game.status !== state.game.status ||
      fresh.items.map(i => i.id + ":" + i.found).join() !== state.items.map(i => i.id + ":" + i.found).join();
    state = fresh;
    if (changed) renderGame(); else renderSummary();
  } catch (e) {
    if (e.status === 401) showJoin();
  }
}

function renderSummary() {
  const { game, player, items, score, found } = state;
  $("game-name").textContent = game.name;
  $("player-name").textContent = player.name;
  $("game-status").textContent = game.status;
  $("game-status").className = `badge ${game.status}`;
  $("game-desc").textContent = game.description;
  $("score").textContent = score;
  $("found-count").textContent = found;
  $("item-count").textContent = items.length;
  $("progress-bar").style.width = items.length ? `${(100 * found) / items.length}%` : "0%";
}

function renderGame() {
  renderSummary();
  const open = state.game.status === "active";
  if (state.game.status === "ended") {
    flash($("game-msg"), "This game has ended. Thanks for playing!", "ok");
  }
  const cards = state.items.map((item, idx) => {
    const msg = el("div");
    const input = el("input", { placeholder: "Your answer", maxlength: 500, "aria-label": `Answer for ${item.title}` });
    const form = el("form", {
      class: "row",
      onsubmit: async (ev) => {
        ev.preventDefault();
        if (!input.value.trim()) return;
        try {
          const res = await api("POST", "/api/play/answer", { item_id: item.id, answer: input.value }, auth());
          if (res.correct) {
            state = res;
            renderGame();
            loadLeaderboard();
          } else {
            flash(msg, "Not quite — try again!", "err");
            input.select();
          }
        } catch (e) {
          flash(msg, e.message, "err");
          if (e.status === 401) showJoin();
        }
      },
    }, el("div", { class: "grow" }, input), el("button", { type: "submit" }, "Submit"));

    return el("div", { class: `card item${item.found ? " found" : ""}` },
      el("div", { class: "row spread" },
        el("h3", {}, `${idx + 1}. ${item.title}`),
        el("span", { class: "muted small" }, `${item.points} pt${item.points === 1 ? "" : "s"}`)),
      item.clue ? el("p", { style: "margin-top:8px; white-space:pre-wrap" }, item.clue) : null,
      item.found
        ? el("p", { class: "muted small" }, `Found ${formatTime(item.found_at)}`)
        : (open ? form : null),
      msg);
  });
  if (!cards.length) cards.push(el("p", { class: "muted" }, "No items have been added to this hunt yet."));
  $("items").replaceChildren(...cards);
}

async function loadLeaderboard() {
  if (!state) return;
  try {
    const data = await api("GET", `/api/leaderboard?code=${encodeURIComponent(state.game.code)}`);
    const rows = data.players.map((p, i) =>
      el("tr", { style: p.name.toLowerCase() === state.player.name.toLowerCase() ? "font-weight:600" : null },
        el("td", {}, i + 1), el("td", {}, p.name), el("td", { class: "num" }, p.found), el("td", { class: "num" }, p.score)));
    $("leaderboard").replaceChildren(
      el("tr", {}, el("th", {}, "#"), el("th", {}, "Player"), el("th", { class: "num" }, "Found"), el("th", { class: "num" }, "Score")),
      ...rows);
  } catch (_) { /* leaderboard is best-effort */ }
}

$("join-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const res = await api("POST", "/api/join", { code: $("join-code").value, name: $("join-name").value });
    token = res.token;
    storageSet(TOKEN_KEY, token);
    loadGame();
  } catch (e) {
    flash($("join-msg"), e.message, "err");
  }
});

$("leave-btn").addEventListener("click", () => {
  if (confirm("Leave this game? You'll need to join again with a new name to keep playing.")) showJoin();
});
$("refresh-btn").addEventListener("click", () => { loadLeaderboard(); refreshStatus(); });

const codeFromUrl = new URLSearchParams(location.search).get("code");
if (codeFromUrl) $("join-code").value = codeFromUrl.toUpperCase();
if (token) loadGame();
