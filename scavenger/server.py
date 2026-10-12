"""HTTP server: a small JSON API plus the player and admin web pages.

Run with `python -m scavenger` (see README.md for options).
"""

import json
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .store import MAX_PHOTO_BYTES, GameError, Store

STATIC_DIR = Path(__file__).parent / "static"
PAGES = {"/": "index.html", "/admin": "admin.html"}
MAX_BODY = 64 * 1024

ROUTES = []


def route(method, pattern, admin=False, raw=False):
    """Register a handler for METHOD + path regex; named groups become int kwargs.
    With admin=True the handler also gets the `game` that the X-Admin-Key header unlocks.
    With raw=True the handler gets the request body as bytes instead of parsed JSON."""

    def wrap(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), admin, raw, fn))
        return fn

    return wrap


class FileResponse:
    def __init__(self, path, content_type):
        self.path = path
        self.content_type = content_type


class App:
    def __init__(self, store):
        self.store = store


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
    result = app.store.submit_answer(player, item_id, body.get("answer"), body.get("captured_at"))
    result.update(app.store.player_view(player))
    return result


@route("POST", "/api/play/photo", raw=True)
def upload_photo(app, req, body):
    player = req.player()
    try:
        item_id = int(req.query.get("item_id", [""])[0])
    except ValueError:
        raise GameError("item_id must be a number") from None
    result = app.store.submit_photo(player, item_id, body, req.query.get("captured_at", [None])[0])
    result.update(app.store.player_view(player))
    return result


@route("GET", r"/api/play/photos/(?P<photo_id>\d+)")
def player_photo(app, req, body, photo_id):
    return FileResponse(*app.store.photo_file(photo_id, player=req.player()))


@route("GET", "/api/leaderboard")
def leaderboard(app, req, body):
    game = app.store.game_by_code(req.query.get("code", [""])[0])
    rows = app.store.leaderboard(game["id"])
    return {
        "game": {"name": game["name"], "status": game["status"]},
        "players": [{k: r[k] for k in ("name", "score", "found")} for r in rows],
    }


# ----- game admin API --------------------------------------------------------
# There are no accounts: creating a game returns a secret admin key, and whoever
# holds it (sent as the X-Admin-Key header) can manage that one game.


@route("POST", "/api/games")
def create_game(app, req, body):
    return app.store.create_game(body.get("name"), body.get("description", ""))


@route("GET", "/api/manage", admin=True)
def get_game(app, req, body, game):
    return app.store.admin_view(game["id"])


@route("PATCH", "/api/manage", admin=True)
def update_game(app, req, body, game):
    return app.store.update_game(game["id"], body)


@route("DELETE", "/api/manage", admin=True)
def delete_game(app, req, body, game):
    app.store.delete_game(game["id"])
    return {"ok": True}


@route("POST", "/api/manage/reset", admin=True)
def reset_game(app, req, body, game):
    app.store.reset_game(game["id"])
    return {"ok": True}


@route("POST", "/api/manage/items", admin=True)
def add_item(app, req, body, game):
    return app.store.add_item(game["id"], body)


@route("PATCH", r"/api/manage/items/(?P<item_id>\d+)", admin=True)
def update_item(app, req, body, game, item_id):
    return app.store.update_item(game["id"], item_id, body)


@route("DELETE", r"/api/manage/items/(?P<item_id>\d+)", admin=True)
def delete_item(app, req, body, game, item_id):
    app.store.delete_item(game["id"], item_id)
    return {"ok": True}


@route("GET", r"/api/manage/photos/(?P<photo_id>\d+)", admin=True)
def admin_photo(app, req, body, game, photo_id):
    return FileResponse(*app.store.photo_file(photo_id, game_id=game["id"]))


@route("POST", r"/api/manage/photos/(?P<photo_id>\d+)/review", admin=True)
def review_photo(app, req, body, game, photo_id):
    return app.store.review_photo(game["id"], photo_id, body.get("status"))


@route("DELETE", r"/api/manage/players/(?P<player_id>\d+)", admin=True)
def delete_player(app, req, body, game, player_id):
    app.store.delete_player(game["id"], player_id)
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
            path_matched = False
            for m, pattern, admin, raw, fn in ROUTES:
                match = pattern.match(url.path)
                if not match:
                    continue
                path_matched = True
                if m != method:
                    continue
                kwargs = {k: int(v) for k, v in match.groupdict().items()}
                if admin:
                    kwargs["game"] = self.app.store.game_by_admin_key(self.headers.get("X-Admin-Key"))
                body = self.read_body(raw) if method in ("POST", "PATCH") else {}
                result = fn(self.app, self, body, **kwargs)
                if isinstance(result, FileResponse):
                    return self.send_file(result.path, result.content_type, private=True)
                return self.send_json(result)
            if path_matched:
                raise GameError("Method not allowed", HTTPStatus.METHOD_NOT_ALLOWED)
            raise GameError("Not found", HTTPStatus.NOT_FOUND)
        except GameError as e:
            self.send_json({"error": str(e)}, e.status)

    def read_body(self, raw_bytes=False):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise GameError("Bad Content-Length") from None
        if length > (MAX_PHOTO_BYTES if raw_bytes else MAX_BODY):
            self.close_connection = True  # don't try to read the oversized body
            raise GameError("Request body too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        raw = self.rfile.read(length) if length else b""
        if raw_bytes:
            return raw
        raw = raw or b"{}"
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
        self.send_file(file, mimetypes.guess_type(file.name)[0] or "application/octet-stream")

    def send_file(self, path, content_type, private=False):
        data = Path(path).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        # Pages and scripts revalidate so phones pick up new versions; photos are per-user.
        self.send_header("Cache-Control", "private, max-age=86400" if private else "no-cache")
        self.end_headers()
        self.wfile.write(data)


def make_server(host, port, db_path, quiet=False, photo_dir=None, ssl_context=None):
    app = App(Store(db_path, photo_dir))
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    if ssl_context:
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    server.quiet = quiet
    return server
