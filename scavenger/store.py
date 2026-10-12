"""SQLite-backed storage and game rules for the scavenger hunt."""

import math
import os
import re
import secrets
import sqlite3
import string
import tempfile
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    code         TEXT NOT NULL UNIQUE,
    admin_key    TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'ended')),
    auto_approve INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id  INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    title    TEXT NOT NULL,
    clue     TEXT NOT NULL DEFAULT '',
    kind     TEXT NOT NULL DEFAULT 'answer' CHECK (kind IN ('answer', 'photo')),
    answer   TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS photos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id    INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    captured_at  REAL NOT NULL,
    submitted_at REAL NOT NULL,
    reviewed_at  REAL
);
"""

# Columns added after the first release, for databases created before them.
MIGRATIONS = [
    ("games", "auto_approve", "INTEGER NOT NULL DEFAULT 0"),
    ("items", "kind", "TEXT NOT NULL DEFAULT 'answer'"),
    ("games", "admin_key", "TEXT"),  # backfilled below
]

STATUSES = ("draft", "active", "ended")
ITEM_KINDS = ("answer", "photo")
CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "O0I1")
MAX_PHOTO_BYTES = 8 * 1024 * 1024
PHOTO_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


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


def photo_extension(data):
    """Identify an uploaded image by its leading bytes; None if unsupported."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def new_admin_key():
    """The secret in a game's admin link; whoever has it can manage that game."""
    return secrets.token_urlsafe(18)


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


def _event_time(captured_at, earliest):
    """When a find happened. Phones that were offline report when the player
    actually answered; trust that, but never before they joined or in the future."""
    now = time.time()
    if isinstance(captured_at, str):
        try:
            captured_at = float(captured_at)
        except ValueError:
            captured_at = None
    if isinstance(captured_at, bool) or not isinstance(captured_at, (int, float)) or not math.isfinite(captured_at):
        return now
    return min(max(float(captured_at), earliest), now)


class Store:
    def __init__(self, path=":memory:", photo_dir=None):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            columns = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        for (game_id,) in self.conn.execute("SELECT id FROM games WHERE admin_key IS NULL").fetchall():
            self.conn.execute("UPDATE games SET admin_key = ? WHERE id = ?", (new_admin_key(), game_id))
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS games_admin_key ON games (admin_key)")
        self.conn.commit()
        self.photo_dir = Path(photo_dir or tempfile.mkdtemp(prefix="scavenger-photos-"))
        self.photo_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def close(self):
        self.conn.close()

    def _q(self, sql, args=()):
        return self.conn.execute(sql, args)

    def _photo_files(self, where, args):
        """Filenames of photos matching `where`, collected before a cascading delete."""
        rows = self._q(
            f"""SELECT ph.filename FROM photos ph
                JOIN players p ON p.id = ph.player_id WHERE {where}""",
            args,
        ).fetchall()
        return [r["filename"] for r in rows]

    def _unlink(self, filenames):
        for name in filenames:
            try:
                (self.photo_dir / name).unlink()
            except FileNotFoundError:
                pass

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
                          (SELECT COUNT(*) FROM players p WHERE p.game_id = g.id) AS player_count,
                          (SELECT COUNT(*) FROM photos ph JOIN players p ON p.id = ph.player_id
                           WHERE p.game_id = g.id AND ph.status = 'pending') AS pending_photos
                   FROM games g ORDER BY g.created_at DESC"""
            ).fetchall()
            return [self._game_dict(r) for r in rows]

    @staticmethod
    def _game_dict(row):
        game = dict(row)
        game["auto_approve"] = bool(game["auto_approve"])
        return game

    def create_game(self, name, description=""):
        name = _clean(name, "Name", 100)
        description = _clean(description, "Description", 2000, required=False)
        with self.lock, self.conn:
            cur = self._q(
                "INSERT INTO games (name, description, code, admin_key, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, description, self._new_code(), new_admin_key(), time.time()),
            )
            return self._game(cur.lastrowid)

    def _game(self, game_id):
        row = self._q("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if not row:
            raise GameError("Game not found", 404)
        return self._game_dict(row)

    def get_game(self, game_id):
        with self.lock:
            return self._game(game_id)

    def game_by_code(self, code):
        with self.lock:
            row = self._q("SELECT * FROM games WHERE code = ?", ((code or "").strip().upper(),)).fetchone()
            if not row:
                raise GameError("No game with that code", 404)
            return self._game_dict(row)

    def game_by_admin_key(self, key):
        with self.lock:
            row = self._q("SELECT * FROM games WHERE admin_key = ?", (key or "",)).fetchone() if key else None
        if not row:
            raise GameError("This admin link isn't valid. The game may have been deleted.", 404)
        return self._game_dict(row)

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
        if "auto_approve" in fields:
            if not isinstance(fields["auto_approve"], bool):
                raise GameError("auto_approve must be true or false")
            updates["auto_approve"] = int(fields["auto_approve"])
        with self.lock, self.conn:
            self._game(game_id)
            if updates:
                assignments = ", ".join(f"{k} = ?" for k in updates)
                self._q(f"UPDATE games SET {assignments} WHERE id = ?", (*updates.values(), game_id))
            return self._game(game_id)

    def delete_game(self, game_id):
        with self.lock, self.conn:
            self._game(game_id)
            files = self._photo_files("p.game_id = ?", (game_id,))
            self._q("DELETE FROM games WHERE id = ?", (game_id,))
        self._unlink(files)

    def reset_game(self, game_id):
        """Remove every player (and their finds and photos) so the hunt can be run again."""
        with self.lock, self.conn:
            self._game(game_id)
            files = self._photo_files("p.game_id = ?", (game_id,))
            self._q("DELETE FROM players WHERE game_id = ?", (game_id,))
        self._unlink(files)

    # ----- items -------------------------------------------------------

    def _item_fields(self, fields, partial):
        out = {}
        specs = {
            "title": lambda v: _clean(v, "Title", 100),
            "clue": lambda v: _clean(v, "Clue", 2000, required=False),
            "kind": self._kind,
            "answer": lambda v: _clean(v, "Answer", 500, required=False),
            "points": lambda v: _int(v, "Points", minimum=0),
            "position": lambda v: _int(v, "Position"),
        }
        for key, check in specs.items():
            if key in fields:
                out[key] = check(fields[key])
            elif not partial and key == "title":
                out[key] = check(None)
        return out

    @staticmethod
    def _kind(value):
        if value not in ITEM_KINDS:
            raise GameError("Kind must be 'answer' or 'photo'")
        return value

    @staticmethod
    def _check_answer(item):
        """Text-answer items need something to match against; photo items don't."""
        if item.get("kind", "answer") == "answer" and not any(
            normalize(a) for a in item.get("answer", "").split("|")
        ):
            raise GameError("Answer is required (letters or numbers) for text-answer items")

    def _item(self, item_id, game_id):
        row = self._q("SELECT * FROM items WHERE id = ? AND game_id = ?", (item_id, game_id)).fetchone()
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
        self._check_answer(values)
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
            return self._item(cur.lastrowid, game_id)

    def update_item(self, game_id, item_id, fields):
        values = self._item_fields(fields, partial=True)
        with self.lock, self.conn:
            self._check_answer({**self._item(item_id, game_id), **values})
            if values:
                assignments = ", ".join(f"{k} = ?" for k in values)
                self._q(f"UPDATE items SET {assignments} WHERE id = ?", (*values.values(), item_id))
            return self._item(item_id, game_id)

    def delete_item(self, game_id, item_id):
        with self.lock, self.conn:
            self._item(item_id, game_id)
            files = self._photo_files("ph.item_id = ?", (item_id,))
            self._q("DELETE FROM items WHERE id = ?", (item_id,))
        self._unlink(files)

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

    def delete_player(self, game_id, player_id):
        with self.lock, self.conn:
            files = self._photo_files("p.id = ? AND p.game_id = ?", (player_id, game_id))
            if not self._q("DELETE FROM players WHERE id = ? AND game_id = ?", (player_id, game_id)).rowcount:
                raise GameError("Player not found", 404)
        self._unlink(files)

    def player_view(self, player):
        """What a player sees: clues (never answers), what they've found and photo statuses."""
        game = self.get_game(player["game_id"])
        with self.lock:
            rows = self._q(
                """SELECT i.id, i.title, i.clue, i.kind, i.points, f.found_at,
                          ph.id AS photo_id, ph.status AS photo_status
                   FROM items i
                   LEFT JOIN finds f ON f.item_id = i.id AND f.player_id = :player
                   LEFT JOIN photos ph ON ph.id = (
                       SELECT id FROM photos WHERE item_id = i.id AND player_id = :player
                       ORDER BY (status = 'approved') DESC, id DESC LIMIT 1)
                   WHERE i.game_id = :game ORDER BY i.position, i.id""",
                {"player": player["id"], "game": game["id"]},
            ).fetchall()
        items = []
        for r in rows:
            item = {k: r[k] for k in ("id", "title", "clue", "kind", "points", "found_at")}
            item["found"] = r["found_at"] is not None
            item["photo"] = {"id": r["photo_id"], "status": r["photo_status"]} if r["photo_id"] else None
            items.append(item)
        return {
            "game": {k: game[k] for k in ("name", "description", "code", "status")},
            "player": {"name": player["name"]},
            "items": items,
            "score": sum(i["points"] for i in items if i["found"]),
            "found": sum(1 for i in items if i["found"]),
        }

    def _playable_item(self, player, item_id, kind):
        game = self._game(player["game_id"])
        if game["status"] != "active":
            raise GameError("This game is not accepting answers right now", 403)
        item = self._q(
            "SELECT * FROM items WHERE id = ? AND game_id = ?", (item_id, game["id"])
        ).fetchone()
        if not item:
            raise GameError("Item not found", 404)
        if item["kind"] != kind:
            raise GameError(
                "This item needs a photo" if item["kind"] == "photo" else "This item needs a typed answer"
            )
        return game, dict(item)

    def submit_answer(self, player, item_id, answer, captured_at=None):
        answer = _clean(answer, "Answer", 500)
        with self.lock, self.conn:
            _, item = self._playable_item(player, item_id, "answer")
            correct = answer_matches(item["answer"], answer)
            if correct:
                self._q(
                    "INSERT OR IGNORE INTO finds (player_id, item_id, found_at) VALUES (?, ?, ?)",
                    (player["id"], item_id, _event_time(captured_at, player["joined_at"])),
                )
        return {"correct": correct}

    # ----- photos ------------------------------------------------------

    def submit_photo(self, player, item_id, data, captured_at=None):
        """Store a photo for review (or approve it straight away if the game auto-approves).
        A new photo replaces any earlier one still waiting for review."""
        if len(data) > MAX_PHOTO_BYTES:
            raise GameError("Photo is too large", 413)
        ext = photo_extension(data)
        if not ext:
            raise GameError("Photo must be a JPEG, PNG or WebP image")
        with self.lock, self.conn:
            game, _ = self._playable_item(player, item_id, "photo")
            key = (player["id"], item_id)
            if self._q("SELECT 1 FROM finds WHERE player_id = ? AND item_id = ?", key).fetchone():
                raise GameError("You've already found this item", 409)
            stale = self._q(
                "SELECT filename FROM photos WHERE player_id = ? AND item_id = ? AND status = 'pending'", key
            ).fetchall()
            self._q("DELETE FROM photos WHERE player_id = ? AND item_id = ? AND status = 'pending'", key)

            filename = secrets.token_hex(16) + ext
            (self.photo_dir / filename).write_bytes(data)
            now = time.time()
            status = "approved" if game["auto_approve"] else "pending"
            captured = _event_time(captured_at, player["joined_at"])
            cur = self._q(
                """INSERT INTO photos (player_id, item_id, filename, status, captured_at, submitted_at, reviewed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (*key, filename, status, captured, now, now if status == "approved" else None),
            )
            if status == "approved":
                self._q("INSERT OR IGNORE INTO finds (player_id, item_id, found_at) VALUES (?, ?, ?)",
                        (*key, captured))
        self._unlink(r["filename"] for r in stale)
        return {"photo_id": cur.lastrowid, "status": status}

    def _photo(self, photo_id):
        return self._q(
            """SELECT ph.*, p.game_id FROM photos ph JOIN players p ON p.id = ph.player_id
               WHERE ph.id = ?""",
            (photo_id,),
        ).fetchone()

    def review_photo(self, game_id, photo_id, status):
        if status not in ("approved", "rejected"):
            raise GameError("Status must be 'approved' or 'rejected'")
        with self.lock, self.conn:
            photo = self._photo(photo_id)
            if photo and photo["game_id"] != game_id:
                photo = None
            if not photo:
                raise GameError("Photo not found", 404)
            key = (photo["player_id"], photo["item_id"])
            self._q("UPDATE photos SET status = ?, reviewed_at = ? WHERE id = ?", (status, time.time(), photo_id))
            if status == "approved":
                # The find counts from when the photo was taken, not when it was reviewed.
                self._q("INSERT OR IGNORE INTO finds (player_id, item_id, found_at) VALUES (?, ?, ?)",
                        (*key, photo["captured_at"]))
            elif not self._q(
                "SELECT 1 FROM photos WHERE player_id = ? AND item_id = ? AND status = 'approved'", key
            ).fetchone():
                self._q("DELETE FROM finds WHERE player_id = ? AND item_id = ?", key)
        return {"id": photo_id, "status": status}

    def photo_file(self, photo_id, player=None, game_id=None):
        """Path and content type of a photo, visible only to the player who took it
        or to the admin of its game. One of `player` or `game_id` is required."""
        if player is None and game_id is None:
            raise ValueError("photo_file needs a player or a game_id")
        with self.lock:
            photo = self._photo(photo_id)
        if (not photo or (player is not None and photo["player_id"] != player["id"])
                or (game_id is not None and photo["game_id"] != game_id)):
            raise GameError("Photo not found", 404)
        path = self.photo_dir / photo["filename"]
        if not path.is_file():
            raise GameError("Photo file is missing", 404)
        return path, PHOTO_TYPES[os.path.splitext(photo["filename"])[1]]

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
            photos = self._q(
                """SELECT ph.id, ph.player_id, p.name AS player_name, ph.item_id, i.title AS item_title,
                          ph.status, ph.captured_at, ph.submitted_at, ph.reviewed_at
                   FROM photos ph
                   JOIN players p ON p.id = ph.player_id
                   JOIN items i ON i.id = ph.item_id
                   WHERE p.game_id = ?
                   ORDER BY (ph.status = 'pending') DESC, ph.submitted_at DESC""",
                (game_id,),
            ).fetchall()
        found_by, pending_by = {}, {}
        for f in finds:
            found_by.setdefault(f["player_id"], []).append(f["item_id"])
        photos = [dict(r) for r in photos]
        for ph in photos:
            if ph["status"] == "pending":
                pending_by.setdefault(ph["player_id"], []).append(ph["item_id"])
        for p in players:
            p["found_items"] = found_by.get(p["id"], [])
            p["pending_items"] = pending_by.get(p["id"], [])
        return {"game": game, "items": items, "players": players, "photos": photos}
