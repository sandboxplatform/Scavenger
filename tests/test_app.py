import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from itertools import count
from unittest import mock

from scavenger.server import make_server
from scavenger.store import GameError, Store, answer_matches

JPEG = b"\xff\xd8\xff\xe0" + b"fake jpeg body"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake png body"


class AnswerMatchingTest(unittest.TestCase):
    def test_ignores_case_punctuation_and_spacing(self):
        self.assertTrue(answer_matches("The Old Oak", "  the old   OAK! "))

    def test_accepts_any_alternative(self):
        self.assertTrue(answer_matches("oak tree|oak", "Oak"))
        self.assertFalse(answer_matches("oak tree|oak", "maple"))

    def test_empty_answer_never_matches(self):
        self.assertFalse(answer_matches("x", "!!!"))


class StoreTest(unittest.TestCase):
    def setUp(self):
        photos = tempfile.TemporaryDirectory()
        self.addCleanup(photos.cleanup)
        self.store = Store(photo_dir=photos.name)
        self.addCleanup(self.store.close)
        self.game = self.store.create_game("Park hunt")
        self.oak = self.store.add_item(self.game["id"], {"title": "Tree", "answer": "oak", "points": 2})
        self.bench = self.store.add_item(self.game["id"], {"title": "Bench", "answer": "red bench"})

    def activate(self):
        self.store.update_game(self.game["id"], {"status": "active"})

    def test_cannot_join_draft_or_ended_game(self):
        with self.assertRaises(GameError):
            self.store.join(self.game["code"], "Ann")
        self.store.update_game(self.game["id"], {"status": "ended"})
        with self.assertRaises(GameError):
            self.store.join(self.game["code"], "Ann")

    def test_names_unique_per_game_case_insensitive(self):
        self.activate()
        self.store.join(self.game["code"], "Ann")
        with self.assertRaises(GameError) as ctx:
            self.store.join(self.game["code"].lower(), "ANN")
        self.assertEqual(ctx.exception.status, 409)

    def test_scoring_and_leaderboard(self):
        self.activate()
        ann = self.store.join(self.game["code"], "Ann")
        bob = self.store.join(self.game["code"], "Bob")
        self.assertFalse(self.store.submit_answer(ann, self.oak["id"], "maple")["correct"])
        self.assertTrue(self.store.submit_answer(bob, self.bench["id"], "Red Bench")["correct"])
        self.assertTrue(self.store.submit_answer(ann, self.oak["id"], "OAK")["correct"])
        # Re-submitting a found item doesn't double count.
        self.store.submit_answer(ann, self.oak["id"], "oak")

        view = self.store.player_view(ann)
        self.assertEqual((view["score"], view["found"]), (2, 1))
        self.assertNotIn("answer", view["items"][0])

        board = self.store.leaderboard(self.game["id"])
        self.assertEqual([(p["name"], p["score"]) for p in board], [("Ann", 2), ("Bob", 1)])

    @mock.patch("scavenger.store.time.time", side_effect=count(1000))
    def test_ties_go_to_whoever_got_there_first(self, _clock):
        self.activate()
        ann = self.store.join(self.game["code"], "Ann")
        bob = self.store.join(self.game["code"], "Bob")
        self.store.submit_answer(bob, self.bench["id"], "red bench")
        self.store.submit_answer(ann, self.bench["id"], "red bench")
        board = self.store.leaderboard(self.game["id"])
        self.assertEqual([p["name"] for p in board], ["Bob", "Ann"])

    def test_answers_rejected_once_game_ends(self):
        self.activate()
        ann = self.store.join(self.game["code"], "Ann")
        self.store.update_game(self.game["id"], {"status": "ended"})
        with self.assertRaises(GameError):
            self.store.submit_answer(ann, self.oak["id"], "oak")

    def test_cannot_answer_item_from_another_game(self):
        other = self.store.create_game("Other")
        foreign = self.store.add_item(other["id"], {"title": "X", "answer": "x"})
        self.activate()
        ann = self.store.join(self.game["code"], "Ann")
        with self.assertRaises(GameError) as ctx:
            self.store.submit_answer(ann, foreign["id"], "x")
        self.assertEqual(ctx.exception.status, 404)

    def test_reset_and_delete_cascade(self):
        self.activate()
        ann = self.store.join(self.game["code"], "Ann")
        self.store.submit_answer(ann, self.oak["id"], "oak")
        self.store.reset_game(self.game["id"])
        self.assertEqual(self.store.leaderboard(self.game["id"]), [])
        self.store.delete_game(self.game["id"])
        self.assertEqual(self.store.list_games(), [])
        self.assertEqual(self.store.items(self.game["id"]), [])

    def test_item_validation(self):
        with self.assertRaises(GameError):
            self.store.add_item(self.game["id"], {"title": "No answer"})
        with self.assertRaises(GameError):
            self.store.add_item(self.game["id"], {"title": "T", "answer": "a", "points": -1})
        with self.assertRaises(GameError):
            self.store.update_item(self.oak["id"], {"answer": "?!"})


    def photo_files(self):
        return sorted(p.name for p in self.store.photo_dir.iterdir())

    def test_photo_items_need_no_answer_but_text_items_do(self):
        item = self.store.add_item(self.game["id"], {"title": "Selfie", "kind": "photo"})
        self.assertEqual(item["kind"], "photo")
        with self.assertRaises(GameError):
            self.store.update_item(item["id"], {"kind": "answer"})
        with self.assertRaises(GameError):
            self.store.add_item(self.game["id"], {"title": "T", "kind": "video"})

    def test_photo_review_flow(self):
        item = self.store.add_item(self.game["id"], {"title": "Duck", "kind": "photo", "points": 5})
        self.activate()
        ann = self.store.join(self.game["code"], "Ann")

        with self.assertRaises(GameError):
            self.store.submit_photo(ann, item["id"], b"not an image")
        with self.assertRaises(GameError):
            self.store.submit_answer(ann, item["id"], "duck")
        with self.assertRaises(GameError):
            self.store.submit_photo(ann, self.oak["id"], JPEG)

        first = self.store.submit_photo(ann, item["id"], JPEG)
        self.assertEqual(first["status"], "pending")
        second = self.store.submit_photo(ann, item["id"], PNG, captured_at=time.time() - 5)
        # The retake replaces the photo still waiting for review, file included.
        self.assertEqual(len(self.photo_files()), 1)
        self.assertTrue(self.photo_files()[0].endswith(".png"))
        view = self.store.player_view(ann)
        self.assertEqual(view["items"][2]["photo"], {"id": second["photo_id"], "status": "pending"})
        self.assertEqual(view["score"], 0)

        with self.assertRaises(GameError):
            self.store.photo_file(first["photo_id"])
        path, ctype = self.store.photo_file(second["photo_id"], player=ann)
        self.assertEqual((path.read_bytes(), ctype), (PNG, "image/png"))

        self.store.review_photo(second["photo_id"], "approved")
        view = self.store.player_view(ann)
        self.assertEqual(view["score"], 5)
        admin = self.store.admin_view(self.game["id"])
        self.assertEqual(admin["players"][0]["found_items"], [item["id"]])
        self.assertEqual(admin["photos"][0]["status"], "approved")
        with self.assertRaises(GameError) as ctx:
            self.store.submit_photo(ann, item["id"], JPEG)
        self.assertEqual(ctx.exception.status, 409)

        self.store.review_photo(second["photo_id"], "rejected")
        self.assertEqual(self.store.player_view(ann)["score"], 0)
        self.assertEqual(self.store.player_view(ann)["items"][2]["photo"]["status"], "rejected")

    def test_other_players_cannot_see_your_photo(self):
        item = self.store.add_item(self.game["id"], {"title": "Duck", "kind": "photo"})
        self.activate()
        ann = self.store.join(self.game["code"], "Ann")
        bob = self.store.join(self.game["code"], "Bob")
        photo = self.store.submit_photo(ann, item["id"], JPEG)
        with self.assertRaises(GameError):
            self.store.photo_file(photo["photo_id"], player=bob)

    def test_auto_approve(self):
        item = self.store.add_item(self.game["id"], {"title": "Duck", "kind": "photo", "points": 3})
        self.store.update_game(self.game["id"], {"status": "active", "auto_approve": True})
        ann = self.store.join(self.game["code"], "Ann")
        self.assertEqual(self.store.submit_photo(ann, item["id"], JPEG)["status"], "approved")
        self.assertEqual(self.store.player_view(ann)["score"], 3)

    def test_photo_files_are_deleted_with_their_players(self):
        item = self.store.add_item(self.game["id"], {"title": "Duck", "kind": "photo"})
        self.activate()
        ann = self.store.join(self.game["code"], "Ann")
        bob = self.store.join(self.game["code"], "Bob")
        self.store.submit_photo(ann, item["id"], JPEG)
        self.store.submit_photo(bob, item["id"], JPEG)
        self.store.delete_player(ann["id"])
        self.assertEqual(len(self.photo_files()), 1)
        self.store.reset_game(self.game["id"])
        self.assertEqual(self.photo_files(), [])

    def test_offline_finds_keep_their_original_time(self):
        self.activate()
        ann = self.store.join(self.game["code"], "Ann")
        bob = self.store.join(self.game["code"], "Bob")
        # Bob answered first while offline but synced later; he still wins the tie.
        self.store.submit_answer(ann, self.bench["id"], "red bench")
        self.store.submit_answer(bob, self.bench["id"], "red bench", captured_at=ann["joined_at"] + 0.0001)
        self.assertEqual([p["name"] for p in self.store.leaderboard(self.game["id"])], ["Bob", "Ann"])

    def test_offline_times_are_clamped(self):
        self.activate()
        ann = self.store.join(self.game["code"], "Ann")
        self.store.submit_answer(ann, self.oak["id"], "oak", captured_at=0)  # before joining
        self.store.submit_answer(ann, self.bench["id"], "red bench", captured_at=time.time() + 3600)  # future
        found = {i["id"]: i["found_at"] for i in self.store.player_view(ann)["items"]}
        self.assertEqual(found[self.oak["id"]], ann["joined_at"])
        self.assertLessEqual(found[self.bench["id"]], time.time())


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.photos = tempfile.TemporaryDirectory()
        cls.server = make_server("127.0.0.1", 0, ":memory:", "secret", quiet=True, photo_dir=cls.photos.name)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.RequestHandlerClass.app.store.close()
        cls.photos.cleanup()

    def call(self, method, path, body=None, headers=None, raw=False):
        if isinstance(body, bytes):
            data, ctype = body, "image/jpeg"
        else:
            data, ctype = (json.dumps(body).encode(), "application/json") if body is not None else (None, None)
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req) as res:
                payload = res.read()
                return res.status, payload if raw else json.loads(payload)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def admin(self):
        status, body = self.call("POST", "/api/admin/login", {"password": "secret"})
        self.assertEqual(status, 200)
        return {"X-Admin-Token": body["token"]}

    def test_admin_endpoints_require_login(self):
        self.assertEqual(self.call("GET", "/api/admin/games")[0], 401)
        self.assertEqual(self.call("GET", "/api/admin/games", headers={"X-Admin-Token": "nope"})[0], 401)
        self.assertEqual(self.call("POST", "/api/admin/login", {"password": "wrong"})[0], 401)

    def test_full_game_flow(self):
        admin = self.admin()
        _, game = self.call("POST", "/api/admin/games", {"name": "Office hunt"}, admin)
        gid = game["id"]
        _, item = self.call("POST", f"/api/admin/games/{gid}/items",
                            {"title": "Mug", "clue": "Coffee lives here", "answer": "kitchen", "points": 3}, admin)

        self.assertEqual(self.call("POST", "/api/join", {"code": game["code"], "name": "Ann"})[0], 403)
        self.call("PATCH", f"/api/admin/games/{gid}", {"status": "active"}, admin)
        status, joined = self.call("POST", "/api/join", {"code": game["code"], "name": "Ann"})
        self.assertEqual(status, 200)
        player = {"X-Player-Token": joined["token"]}

        _, view = self.call("GET", "/api/play", headers=player)
        self.assertEqual(view["items"][0]["clue"], "Coffee lives here")
        self.assertNotIn("answer", view["items"][0])

        _, res = self.call("POST", "/api/play/answer", {"item_id": item["id"], "answer": "Kitchen!"}, player)
        self.assertTrue(res["correct"])
        self.assertEqual(res["score"], 3)

        _, board = self.call("GET", f"/api/leaderboard?code={game['code']}")
        self.assertEqual(board["players"], [{"name": "Ann", "score": 3, "found": 1}])

        _, detail = self.call("GET", f"/api/admin/games/{gid}", headers=admin)
        self.assertEqual(detail["players"][0]["found_items"], [item["id"]])

        self.assertEqual(self.call("DELETE", f"/api/admin/players/{detail['players'][0]['id']}", headers=admin)[0], 200)
        self.assertEqual(self.call("GET", "/api/play", headers=player)[0], 401)
        self.assertEqual(self.call("DELETE", f"/api/admin/games/{gid}", headers=admin)[0], 200)

    def test_photo_upload_and_review(self):
        admin = self.admin()
        _, game = self.call("POST", "/api/admin/games", {"name": "Photo hunt"}, admin)
        _, item = self.call("POST", f"/api/admin/games/{game['id']}/items",
                            {"title": "Duck", "kind": "photo", "points": 4}, admin)
        self.call("PATCH", f"/api/admin/games/{game['id']}", {"status": "active"}, admin)
        ann = {"X-Player-Token": self.call("POST", "/api/join", {"code": game["code"], "name": "Ann"})[1]["token"]}
        bob = {"X-Player-Token": self.call("POST", "/api/join", {"code": game["code"], "name": "Bob"})[1]["token"]}

        status, res = self.call("POST", f"/api/play/photo?item_id={item['id']}", b"plain text", ann)
        self.assertEqual(status, 400)
        status, res = self.call("POST", f"/api/play/photo?item_id={item['id']}&captured_at=1", JPEG, ann)
        self.assertEqual((status, res["status"]), (200, "pending"))
        photo_id = res["photo_id"]

        self.assertEqual(self.call("GET", f"/api/play/photos/{photo_id}", headers=ann, raw=True), (200, JPEG))
        self.assertEqual(self.call("GET", f"/api/play/photos/{photo_id}", headers=bob)[0], 404)
        self.assertEqual(self.call("GET", f"/api/admin/photos/{photo_id}", headers=admin, raw=True), (200, JPEG))
        self.assertEqual(self.call("GET", f"/api/admin/photos/{photo_id}")[0], 401)

        _, detail = self.call("GET", f"/api/admin/games/{game['id']}", headers=admin)
        self.assertEqual(detail["players"][0]["pending_items"], [item["id"]])
        self.assertEqual(self.call("POST", f"/api/admin/photos/{photo_id}/review", {"status": "approved"}, admin)[0], 200)
        _, board = self.call("GET", f"/api/leaderboard?code={game['code']}")
        self.assertEqual(board["players"][0], {"name": "Ann", "score": 4, "found": 1})

    def test_bad_requests(self):
        admin = self.admin()
        self.assertEqual(self.call("PATCH", "/api/admin/games/999", {"status": "active"}, admin)[0], 404)
        _, game = self.call("POST", "/api/admin/games", {"name": "G"}, admin)
        self.assertEqual(self.call("PATCH", f"/api/admin/games/{game['id']}", {"status": "paused"}, admin)[0], 400)
        self.assertEqual(self.call("GET", "/api/join")[0], 405)
        self.assertEqual(self.call("GET", "/api/nothing")[0], 404)

    def test_serves_pages_but_not_outside_static(self):
        for path in ("/", "/admin", "/style.css"):
            with urllib.request.urlopen(self.base + path) as res:
                self.assertEqual(res.status, 200)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/../store.py")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
