"""HTTP server: a small JSON API plus the player and admin web pages.

Run with `python -m scavenger` (see README.md for options).
"""

import hmac
import json
import mimetypes
import re
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .store import GameError, Store

STATIC_DIR = Path(__file__).parent / "static"
PAGES = {"/": "index.html", "/admin": "admin.html"}
MAX_BODY = 64 * 1024

ROUTES = []


def route(method, pattern, admin=False):
    """Register a handler for METHOD + path regex; named groups become int kwargs."""

    def wrap(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), admin, fn))
        return fn

    return wrap


class App:
    def __init__(self, store, admin_password):
        self.store = store
        self.admin_password = admin_password
        self.admin_tokens = set()

    def check_admin(self, token):
        if not token or not any(hmac.compare_digest(token, t) for t in self.admin_tokens):
            raise GameError("Admin login required", 401)


# ----- player API ----------------------------------------------------------


@route("POST", "/api/join")
def join(app, req, body):
    player = app.store.join(body.get("code"), body.get("name"))
    return {"token": player["token"]}


@route("GET", "/api/play")
def play(app, req, body):
    return app.store.player_view(req.player())


@route("POST", "/api/play/answer")
def answer(app, req, body):
    player = req.player()
    item_id = body.get("item_id")
    if not isinstance(item_id, int) or isinstance(item_id, bool):
        raise GameError("item_id must be a number")
    result = app.store.submit_answer(player, item_id, body.get("answer"))
    result.update(app.store.player_view(player))
    return result


@route("GET", "/api/leaderboard")
def leaderboard(app, req, body):
    game = app.store.game_by_code(req.query.get("code", [""])[0])
    rows = app.store.leaderboard(game["id"])
    return {
        "game": {"name": game["name"], "status": game["status"]},
        "players": [{k: r[k] for k in ("name", "score", "found")} for r in rows],
    }


# ----- admin API -----------------------------------------------------------


@route("POST", "/api/admin/login")
def admin_login(app, req, body):
    password = body.get("password")
    if not isinstance(password, str) or not hmac.compare_digest(
        password.encode(), app.admin_password.encode()
    ):
        raise GameError("Wrong password", 401)
    token = secrets.token_urlsafe(24)
    app.admin_tokens.add(token)
    return {"token": token}


@route("POST", "/api/admin/logout", admin=True)
def admin_logout(app, req, body):
    app.admin_tokens.discard(req.headers.get("X-Admin-Token"))
    return {"ok": True}


@route("GET", "/api/admin/games", admin=True)
def list_games(app, req, body):
    return {"games": app.store.list_games()}


@route("POST", "/api/admin/games", admin=True)
def create_game(app, req, body):
    return app.store.create_game(body.get("name"), body.get("description", ""))


@route("GET", r"/api/admin/games/(?P<game_id>\d+)", admin=True)
def get_game(app, req, body, game_id):
    return app.store.admin_view(game_id)


@route("PATCH", r"/api/admin/games/(?P<game_id>\d+)", admin=True)
def update_game(app, req, body, game_id):
    return app.store.update_game(game_id, body)


@route("DELETE", r"/api/admin/games/(?P<game_id>\d+)", admin=True)
def delete_game(app, req, body, game_id):
    app.store.delete_game(game_id)
    return {"ok": True}


@route("POST", r"/api/admin/games/(?P<game_id>\d+)/reset", admin=True)
def reset_game(app, req, body, game_id):
    app.store.reset_game(game_id)
    return {"ok": True}


@route("POST", r"/api/admin/games/(?P<game_id>\d+)/items", admin=True)
def add_item(app, req, body, game_id):
    return app.store.add_item(game_id, body)


@route("PATCH", r"/api/admin/items/(?P<item_id>\d+)", admin=True)
def update_item(app, req, body, item_id):
    return app.store.update_item(item_id, body)


@route("DELETE", r"/api/admin/items/(?P<item_id>\d+)", admin=True)
def delete_item(app, req, body, item_id):
    app.store.delete_item(item_id)
    return {"ok": True}


@route("DELETE", r"/api/admin/players/(?P<player_id>\d+)", admin=True)
def delete_player(app, req, body, player_id):
    app.store.delete_player(player_id)
    return {"ok": True}


# ----- HTTP plumbing -------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    app = None  # set by make_server
    server_version = "Scavenger/1.0"

    def log_message(self, fmt, *args):
        if not getattr(self.server, "quiet", False):
            super().log_message(fmt, *args)

    def player(self):
        return self.app.store.player_by_token(self.headers.get("X-Player-Token"))

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        url = urlparse(self.path)
        self.query = parse_qs(url.query)
        if not url.path.startswith("/api/"):
            if method == "GET":
                return self.serve_static(url.path)
            return self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        try:
            body = self.read_body() if method in ("POST", "PATCH") else {}
            path_matched = False
            for m, pattern, admin, fn in ROUTES:
                match = pattern.match(url.path)
                if not match:
                    continue
                path_matched = True
                if m != method:
                    continue
                if admin:
                    self.app.check_admin(self.headers.get("X-Admin-Token"))
                kwargs = {k: int(v) for k, v in match.groupdict().items()}
                return self.send_json(fn(self.app, self, body, **kwargs))
            if path_matched:
                raise GameError("Method not allowed", HTTPStatus.METHOD_NOT_ALLOWED)
            raise GameError("Not found", HTTPStatus.NOT_FOUND)
        except GameError as e:
            self.send_json({"error": str(e)}, e.status)

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise GameError("Bad Content-Length") from None
        if length > MAX_BODY:
            raise GameError("Request body too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise GameError("Request body must be JSON") from None
        if not isinstance(body, dict):
            raise GameError("Request body must be a JSON object")
        return body

    def send_json(self, data, status=HTTPStatus.OK):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def serve_static(self, path):
        name = PAGES.get(path) or path.lstrip("/")
        file = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in file.parents or not file.is_file():
            return self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        data = file.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(file.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(host, port, db_path, admin_password, quiet=False):
    app = App(Store(db_path), admin_password)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.quiet = quiet
    return server
