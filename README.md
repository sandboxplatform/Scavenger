# Scavenger Hunt

A small, self-hosted scavenger hunt web app with a **player** page and an **admin** page.
It only needs Python 3.9+, with no third-party packages. Data is stored in a local SQLite file.

## Run it

```bash
SCAVENGER_ADMIN_PASSWORD='choose-a-password' python3 -m scavenger
```

- Players: <http://127.0.0.1:8000/>
- Admin: <http://127.0.0.1:8000/admin>

Options (flags or environment variables):

| Flag     | Env var                    | Default        |
|----------|----------------------------|----------------|
| `--host` | `SCAVENGER_HOST`           | `127.0.0.1`    |
| `--port` | `SCAVENGER_PORT`           | `8000`         |
| `--db`   | `SCAVENGER_DB`             | `scavenger.db` |
| —        | `SCAVENGER_ADMIN_PASSWORD` | `admin` (prints a warning) |

To let phones on the same network join, use `--host 0.0.0.0` and share `http://<your-ip>:8000/?code=XXXXXX`.

## Admin tools

- Create, rename and delete games. Each game gets a 6-character join code and a shareable player link.
- Set a game's status:
  - **Draft**: you can set it up, but players can't join.
  - **Active**: players can join and submit answers.
  - **Ended**: answers are locked and results are final.
- Add, edit, reorder and delete items, each with a title, clue, answer and point value.
  - Answers are case-insensitive and ignore punctuation and extra spaces.
  - To accept more than one answer, separate them with `|`, for example `oak tree|oak`.
- See a live progress grid of who has found which item, plus scores and the time of each player's last find.
- Remove a single player, or **reset** the game to clear all players and keep the items.

## Player tools

- Join with a game code and a name or team name. Names must be unique within a game.
- See each clue and submit an answer. The answers themselves are never sent to the browser.
- Track your score and progress, and see the live leaderboard. Ties go to whoever reached the score first.
- Your session is remembered in the browser, so refreshing the page doesn't lose your progress.

## Tests

```bash
python3 -m unittest discover -s tests
```

## Layout

```
scavenger/
  __main__.py   command-line entry point
  server.py     HTTP server and JSON API routes
  store.py      SQLite storage and game rules
  static/       player (index.html, player.js) and admin (admin.html, admin.js) pages
tests/          unit + HTTP API tests
```

## Notes and limitations

- There is one shared admin password. Admin logins are kept in memory, so restarting the server logs admins out. Player sessions are stored in the database and survive a restart.
- The built-in server is meant for small events on a trusted network. To run it on the public internet, put it behind HTTPS (for example, a reverse proxy).
