import json
import threading
import unittest
import urllib.error
import urllib.request
from itertools import count
from unittest import mock

from scavenger.server import make_server
from scavenger.store import GameError, Store, answer_matches


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
        self.store = Store()
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


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0, ":memory:", "secret", quiet=True)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.RequestHandlerClass.app.store.close()

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
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
