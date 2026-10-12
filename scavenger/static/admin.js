const ADMIN_KEY = "scavenger.adminToken";
let adminToken = storageGet(ADMIN_KEY);
let current = null; // admin view of the open game
let pollTimer = null;

const views = ["login-view", "list-view", "game-view"];
function show(id) {
  for (const v of views) $(v).classList.toggle("hidden", v !== id);
}

async function adminApi(method, path, body) {
  try {
    return await api(method, path, body, { "X-Admin-Token": adminToken || "" });
  } catch (e) {
    if (e.status === 401 && path !== "/api/admin/login") logout(false);
    throw e;
  }
}

function logout(callServer = true) {
  if (callServer && adminToken) adminApi("POST", "/api/admin/logout", {}).catch(() => {});
  adminToken = null;
  storageSet(ADMIN_KEY, null);
  clearInterval(pollTimer);
  current = null;
  history.replaceState(null, "", "/admin");
  show("login-view");
}

// ----- game list -----------------------------------------------------------

async function showList() {
  clearInterval(pollTimer);
  current = null;
  history.replaceState(null, "", "/admin");
  show("list-view");
  let data;
  try {
    data = await adminApi("GET", "/api/admin/games");
  } catch (e) {
    return flash($("games"), e.message, "err");
  }
  if (!data.games.length) {
    return $("games").replaceChildren(el("p", { class: "muted" }, "No games yet — create one above."));
  }
  $("games").replaceChildren(...data.games.map(g =>
    el("div", { class: "list-item row spread" },
      el("div", {},
        el("button", { class: "link", onclick: () => openGame(g.id) }, g.name), " ",
        el("span", { class: `badge ${g.status}` }, g.status),
        el("div", { class: "small muted" },
          `Code ${g.code} · ${g.item_count} item${g.item_count === 1 ? "" : "s"} · ${g.player_count} player${g.player_count === 1 ? "" : "s"}`)),
      el("button", { class: "secondary", onclick: () => openGame(g.id) }, "Manage"))));
}

$("create-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const game = await adminApi("POST", "/api/admin/games", { name: $("new-name").value, description: $("new-desc").value });
    $("create-form").reset();
    openGame(game.id);
  } catch (e) {
    flash($("create-msg"), e.message, "err");
  }
});

// ----- single game ---------------------------------------------------------

async function openGame(id) {
  try {
    current = await adminApi("GET", `/api/admin/games/${id}`);
  } catch (e) {
    if (e.status === 404) return showList();
    return flash($("game-msg"), e.message, "err");
  }
  history.replaceState(null, "", `/admin?game=${id}`);
  show("game-view");
  renderSettings();
  renderItems();
  renderPlayers();
  clearInterval(pollTimer);
  pollTimer = setInterval(refreshPlayers, 10000);
}

async function refreshPlayers() {
  if (!current) return;
  try {
    const fresh = await adminApi("GET", `/api/admin/games/${current.game.id}`);
    current.players = fresh.players;
    renderPlayers();
  } catch (_) {}
}

function renderSettings() {
  const g = current.game;
  $("game-code").textContent = g.code;
  const link = `${location.origin}/?code=${g.code}`;
  $("join-link").href = link;
  $("join-link").textContent = link;
  $("g-name").value = g.name;
  $("g-desc").value = g.description;
  $("g-status").value = g.status;
}

$("game-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    current.game = await adminApi("PATCH", `/api/admin/games/${current.game.id}`, {
      name: $("g-name").value, description: $("g-desc").value, status: $("g-status").value,
    });
    renderSettings();
    flash($("game-msg"), "Settings saved.");
  } catch (e) {
    flash($("game-msg"), e.message, "err");
  }
});

$("reset-btn").addEventListener("click", async () => {
  if (!confirm("Remove ALL players and their progress from this game? Items are kept.")) return;
  try {
    await adminApi("POST", `/api/admin/games/${current.game.id}/reset`, {});
    await openGame(current.game.id);
    flash($("game-msg"), "All players removed.");
  } catch (e) {
    flash($("game-msg"), e.message, "err");
  }
});

$("delete-btn").addEventListener("click", async () => {
  if (!confirm(`Permanently delete "${current.game.name}" with all its items and players?`)) return;
  try {
    await adminApi("DELETE", `/api/admin/games/${current.game.id}`);
    showList();
  } catch (e) {
    flash($("game-msg"), e.message, "err");
  }
});

function renderItems() {
  if (!current.items.length) {
    return $("items").replaceChildren(el("p", { class: "muted" }, "No items yet — add one below."));
  }
  $("items").replaceChildren(...current.items.map((item, idx) => itemEditor(item, idx)));
}

function itemEditor(item, idx) {
  const f = {
    title: el("input", { value: item.title, maxlength: 100, required: true, "aria-label": "Title" }),
    clue: el("textarea", { maxlength: 2000, "aria-label": "Clue" }),
    answer: el("input", { value: item.answer, maxlength: 500, required: true, "aria-label": "Answer" }),
    points: el("input", { type: "number", min: 0, value: item.points, required: true, "aria-label": "Points" }),
    position: el("input", { type: "number", value: item.position, required: true, "aria-label": "Order" }),
  };
  f.clue.value = item.clue;
  const msg = el("div");
  const body = el("form", {
    class: "stack hidden",
    style: "margin-top:10px",
    onsubmit: async (ev) => {
      ev.preventDefault();
      try {
        await adminApi("PATCH", `/api/admin/items/${item.id}`, {
          title: f.title.value, clue: f.clue.value, answer: f.answer.value,
          points: Number(f.points.value), position: Number(f.position.value),
        });
        await openGame(current.game.id);
        flash($("game-msg"), "Item saved.");
      } catch (e) {
        flash(msg, e.message, "err");
      }
    },
  },
    el("div", {}, el("label", {}, "Title"), f.title),
    el("div", {}, el("label", {}, "Clue"), f.clue),
    el("div", { class: "row" },
      el("div", { class: "grow" }, el("label", {}, "Answer"), f.answer),
      el("div", { style: "width:90px" }, el("label", {}, "Points"), f.points),
      el("div", { style: "width:90px" }, el("label", {}, "Order"), f.position)),
    el("div", { class: "row" },
      el("button", { type: "submit" }, "Save"),
      el("button", {
        type: "button", class: "danger",
        onclick: async () => {
          if (!confirm(`Delete item "${item.title}"? Players lose points for it.`)) return;
          try {
            await adminApi("DELETE", `/api/admin/items/${item.id}`);
            await openGame(current.game.id);
          } catch (e) {
            flash(msg, e.message, "err");
          }
        },
      }, "Delete")),
    msg);

  return el("div", { class: "list-item" },
    el("div", { class: "row spread" },
      el("div", {},
        el("strong", {}, `${idx + 1}. ${item.title}`),
        el("div", { class: "small muted" }, `Answer: ${item.answer} · ${item.points} pt${item.points === 1 ? "" : "s"} · found by `,
          el("span", { "data-found-by": item.id }, foundBy(item.id)))),
      el("button", { class: "secondary", onclick: () => body.classList.toggle("hidden") }, "Edit")),
    body);
}

$("item-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await adminApi("POST", `/api/admin/games/${current.game.id}/items`, {
      title: $("i-title").value, clue: $("i-clue").value,
      answer: $("i-answer").value, points: Number($("i-points").value),
    });
    $("item-form").reset();
    await openGame(current.game.id);
    flash($("item-msg"), "Item added.");
  } catch (e) {
    flash($("item-msg"), e.message, "err");
  }
});

function foundBy(itemId) {
  return current.players.filter(p => p.found_items.includes(itemId)).length;
}

function renderPlayers() {
  const items = current.items;
  document.querySelectorAll("[data-found-by]").forEach(span => {
    span.textContent = foundBy(Number(span.dataset.foundBy));
  });
  if (!current.players.length) {
    return $("players").replaceChildren(el("tr", {}, el("td", { class: "muted" },
      current.game.status === "active" ? "No players yet. Share the code above." : "No players yet. Set the game to Active so players can join.")));
  }
  const header = el("tr", {},
    el("th", {}, "#"), el("th", {}, "Player"),
    ...items.map((it, i) => el("th", { title: it.title }, i + 1)),
    el("th", { class: "num" }, "Score"), el("th", {}, "Last find"), el("th", {}));
  const rows = current.players.map((p, rank) => el("tr", {},
    el("td", {}, rank + 1),
    el("td", {}, p.name),
    ...items.map(it => el("td", { title: it.title }, p.found_items.includes(it.id) ? "✓" : "·")),
    el("td", { class: "num" }, p.score),
    el("td", { class: "small muted" }, formatTime(p.last_found)),
    el("td", {}, el("button", {
      class: "link",
      onclick: async () => {
        if (!confirm(`Remove ${p.name} from the game?`)) return;
        try {
          await adminApi("DELETE", `/api/admin/players/${p.id}`);
          refreshPlayers();
        } catch (e) {
          flash($("game-msg"), e.message, "err");
        }
      },
    }, "Remove"))));
  $("players").replaceChildren(header, ...rows);
}

$("refresh-btn").addEventListener("click", refreshPlayers);
$("back-btn").addEventListener("click", showList);
document.querySelectorAll("[data-logout]").forEach(b => b.addEventListener("click", () => logout()));

$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const res = await adminApi("POST", "/api/admin/login", { password: $("password").value });
    adminToken = res.token;
    storageSet(ADMIN_KEY, adminToken);
    $("login-form").reset();
    showList();
  } catch (e) {
    flash($("login-msg"), e.message, "err");
  }
});

// ----- start -----------------------------------------------------------------

if (!adminToken) {
  show("login-view");
} else {
  const gameId = new URLSearchParams(location.search).get("game");
  if (gameId) openGame(gameId); else showList();
}
