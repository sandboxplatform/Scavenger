"""SQLite-backed storage and game rules for the scavenger hunt."""

import re
import secrets
import sqlite3
import string
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    code        TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'ended')),
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id  INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    title    TEXT NOT NULL,
    clue     TEXT NOT NULL DEFAULT '',
    answer   TEXT NOT NULL,
    points   INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS players (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id    INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    name       TEXT NOT NULL COLLATE NOCASE,
    token      TEXT NOT NULL UNIQUE,
    joined_at  REAL NOT NULL,
    UNIQUE (game_id, name)
);

CREATE TABLE IF NOT EXISTS finds (
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    found_at  REAL NOT NULL,
    PRIMARY KEY (player_id, item_id)
);
"""

STATUSES = ("draft", "active", "ended")
CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "O0I1")


class GameError(Exception):
    """A request that breaks a game rule; the message is safe to show users."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def normalize(text):
    """Lowercase, drop punctuation and collapse whitespace for answer matching."""
    text = re.sub(r"[^\w\s]", "", text.lower())
    return " ".join(text.split())


def answer_matches(expected, given):
    """`expected` may list several accepted answers separated by '|'."""
    given = normalize(given)
    return bool(given) and any(normalize(opt) == given for opt in expected.split("|"))


def _clean(value, field, max_len=200, required=True):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise GameError(f"{field} must be text")
    value = value.strip()
    if required and not value:
        raise GameError(f"{field} is required")
    if len(value) > max_len:
        raise GameError(f"{field} must be at most {max_len} characters")
    return value


def _int(value, field, minimum=None):
    if isinstance(value, bool):
        raise GameError(f"{field} must be a whole number")
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise GameError(f"{field} must be a whole number") from None
    if minimum is not None and value < minimum:
        raise GameError(f"{field} must be at least {minimum}")
    return value


class Store:
    def __init__(self, path=":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    def close(self):
        self.conn.close()

    def _q(self, sql, args=()):
        return self.conn.execute(sql, args)

    # ----- games -------------------------------------------------------

    def _new_code(self):
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
            if not self._q("SELECT 1 FROM games WHERE code = ?", (code,)).fetchone():
                return code

    def list_games(self):
        with self.lock:
            rows = self._q(
                """SELECT g.*,
                          (SELECT COUNT(*) FROM items i WHERE i.game_id = g.id) AS item_count,
                          (SELECT COUNT(*) FROM players p WHERE p.game_id = g.id) AS player_count
                   FROM games g ORDER BY g.created_at DESC"""
            ).fetchall()
            return [dict(r) for r in rows]

    def create_game(self, name, description=""):
        name = _clean(name, "Name", 100)
        description = _clean(description, "Description", 2000, required=False)
        with self.lock, self.conn:
            cur = self._q(
                "INSERT INTO games (name, description, code, created_at) VALUES (?, ?, ?, ?)",
                (name, description, self._new_code(), time.time()),
            )
            return self._game(cur.lastrowid)

    def _game(self, game_id):
        row = self._q("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if not row:
            raise GameError("Game not found", 404)
        return dict(row)

    def get_game(self, game_id):
        with self.lock:
            return self._game(game_id)

    def game_by_code(self, code):
        with self.lock:
            row = self._q("SELECT * FROM games WHERE code = ?", ((code or "").strip().upper(),)).fetchone()
            if not row:
                raise GameError("No game with that code", 404)
            return dict(row)

    def update_game(self, game_id, fields):
        updates = {}
        if "name" in fields:
            updates["name"] = _clean(fields["name"], "Name", 100)
        if "description" in fields:
            updates["description"] = _clean(fields["description"], "Description", 2000, required=False)
        if "status" in fields:
            if fields["status"] not in STATUSES:
                raise GameError("Status must be one of: " + ", ".join(STATUSES))
            updates["status"] = fields["status"]
        with self.lock, self.conn:
            self._game(game_id)
            if updates:
                assignments = ", ".join(f"{k} = ?" for k in updates)
                self._q(f"UPDATE games SET {assignments} WHERE id = ?", (*updates.values(), game_id))
            return self._game(game_id)

    def delete_game(self, game_id):
        with self.lock, self.conn:
            self._game(game_id)
            self._q("DELETE FROM games WHERE id = ?", (game_id,))

    def reset_game(self, game_id):
        """Remove every player (and their finds) so the hunt can be run again."""
        with self.lock, self.conn:
            self._game(game_id)
            self._q("DELETE FROM players WHERE game_id = ?", (game_id,))

    # ----- items -------------------------------------------------------

    def _item_fields(self, fields, partial):
        out = {}
        specs = {
            "title": lambda v: _clean(v, "Title", 100),
            "clue": lambda v: _clean(v, "Clue", 2000, required=False),
            "answer": lambda v: _clean(v, "Answer", 500),
            "points": lambda v: _int(v, "Points", minimum=0),
            "position": lambda v: _int(v, "Position"),
        }
        for key, check in specs.items():
            if key in fields:
                out[key] = check(fields[key])
            elif not partial and key in ("title", "answer"):
                out[key] = check(None)
        if "answer" in out and not any(normalize(a) for a in out["answer"].split("|")):
            raise GameError("Answer must contain letters or numbers")
        return out

    def _item(self, item_id):
        row = self._q("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise GameError("Item not found", 404)
        return dict(row)

    def items(self, game_id):
        with self.lock:
            rows = self._q(
                "SELECT * FROM items WHERE game_id = ? ORDER BY position, id", (game_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def add_item(self, game_id, fields):
        values = self._item_fields(fields, partial=False)
        with self.lock, self.conn:
            self._game(game_id)
            if "position" not in values:
                last = self._q(
                    "SELECT COALESCE(MAX(position), 0) FROM items WHERE game_id = ?", (game_id,)
                ).fetchone()[0]
                values["position"] = last + 1
            values["game_id"] = game_id
            cols = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            cur = self._q(f"INSERT INTO items ({cols}) VALUES ({marks})", tuple(values.values()))
            return self._item(cur.lastrowid)

    def update_item(self, item_id, fields):
        values = self._item_fields(fields, partial=True)
        with self.lock, self.conn:
            self._item(item_id)
            if values:
                assignments = ", ".join(f"{k} = ?" for k in values)
                self._q(f"UPDATE items SET {assignments} WHERE id = ?", (*values.values(), item_id))
            return self._item(item_id)

    def delete_item(self, item_id):
        with self.lock, self.conn:
            self._item(item_id)
            self._q("DELETE FROM items WHERE id = ?", (item_id,))

    # ----- players -----------------------------------------------------

    def join(self, code, name):
        name = _clean(name, "Name", 40)
        game = self.game_by_code(code)
        if game["status"] != "active":
            raise GameError(
                "This game hasn't started yet" if game["status"] == "draft" else "This game has ended", 403
            )
        with self.lock, self.conn:
            try:
                cur = self._q(
                    "INSERT INTO players (game_id, name, token, joined_at) VALUES (?, ?, ?, ?)",
                    (game["id"], name, secrets.token_urlsafe(24), time.time()),
                )
            except sqlite3.IntegrityError:
                raise GameError("That name is already taken in this game", 409) from None
            return dict(self._q("SELECT * FROM players WHERE id = ?", (cur.lastrowid,)).fetchone())

    def player_by_token(self, token):
        with self.lock:
            row = self._q("SELECT * FROM players WHERE token = ?", (token or "",)).fetchone()
            if not row:
                raise GameError("Your session has expired; please join again", 401)
            return dict(row)

    def delete_player(self, player_id):
        with self.lock, self.conn:
            if not self._q("DELETE FROM players WHERE id = ?", (player_id,)).rowcount:
                raise GameError("Player not found", 404)

    def player_view(self, player):
        """What a player sees: clues (never answers) and which they've found."""
        game = self.get_game(player["game_id"])
        with self.lock:
            rows = self._q(
                """SELECT i.id, i.title, i.clue, i.points, f.found_at
                   FROM items i LEFT JOIN finds f ON f.item_id = i.id AND f.player_id = ?
                   WHERE i.game_id = ? ORDER BY i.position, i.id""",
                (player["id"], game["id"]),
            ).fetchall()
        items = [dict(r, found=r["found_at"] is not None) for r in rows]
        return {
            "game": {k: game[k] for k in ("name", "description", "code", "status")},
            "player": {"name": player["name"]},
            "items": items,
            "score": sum(i["points"] for i in items if i["found"]),
            "found": sum(1 for i in items if i["found"]),
        }

    def submit_answer(self, player, item_id, answer):
        game = self.get_game(player["game_id"])
        if game["status"] != "active":
            raise GameError("This game is not accepting answers right now", 403)
        answer = _clean(answer, "Answer", 500)
        with self.lock, self.conn:
            item = self._q(
                "SELECT * FROM items WHERE id = ? AND game_id = ?", (item_id, game["id"])
            ).fetchone()
            if not item:
                raise GameError("Item not found", 404)
            correct = answer_matches(item["answer"], answer)
            if correct:
                self._q(
                    "INSERT OR IGNORE INTO finds (player_id, item_id, found_at) VALUES (?, ?, ?)",
                    (player["id"], item_id, time.time()),
                )
        return {"correct": correct}

    # ----- reporting ---------------------------------------------------

    def leaderboard(self, game_id):
        """Rank by score, then by who reached that score first."""
        with self.lock:
            rows = self._q(
                """SELECT p.id, p.name, p.joined_at,
                          COALESCE(SUM(i.points), 0) AS score,
                          COUNT(f.item_id) AS found,
                          MAX(f.found_at) AS last_found
                   FROM players p
                   LEFT JOIN finds f ON f.player_id = p.id
                   LEFT JOIN items i ON i.id = f.item_id
                   WHERE p.game_id = ?
                   GROUP BY p.id
                   ORDER BY score DESC, COALESCE(MAX(f.found_at), p.joined_at) ASC, p.id ASC""",
                (game_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def admin_view(self, game_id):
        game = self.get_game(game_id)
        items = self.items(game_id)
        players = self.leaderboard(game_id)
        with self.lock:
            finds = self._q(
                """SELECT f.player_id, f.item_id FROM finds f
                   JOIN players p ON p.id = f.player_id WHERE p.game_id = ?""",
                (game_id,),
            ).fetchall()
        found_by = {}
        for f in finds:
            found_by.setdefault(f["player_id"], []).append(f["item_id"])
        for p in players:
            p["found_items"] = found_by.get(p["id"], [])
        return {"game": game, "items": items, "players": players}
