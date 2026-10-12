# Scavenger Hunt

A small, self-hosted scavenger hunt web app with a **player** page and an **admin** page.
Players can prove each find with a photo taken on their phone, and they can keep playing with no signal.
It only needs Python 3.9+, with no third-party packages. Data is stored in a local SQLite file, and photos are saved in a folder.

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
| `--photos` | `SCAVENGER_PHOTOS`       | `photos/`      |
| `--cert`, `--key` | `SCAVENGER_CERT`, `SCAVENGER_KEY` | none. Serves HTTPS when given (see [Offline mode](#offline-mode)) |
| —        | `SCAVENGER_ADMIN_PASSWORD` | `admin` (prints a warning) |

To let phones on the same network join, use `--host 0.0.0.0` and share `http://<your-ip>:8000/?code=XXXXXX`.

## Admin tools

- Create, rename and delete games. Each game gets a 6-character join code and a shareable player link.
- Set a game's status:
  - **Draft**: you can set it up, but players can't join.
  - **Active**: players can join and submit answers.
  - **Ended**: answers are locked and results are final.
- Add, edit, reorder and delete items. Each item has a title, a clue, a point value, and one of two ways to complete it:
  - **📷 Photo:** the player takes a picture with their phone camera.
  - **Typed answer:** answers are case-insensitive and ignore punctuation and extra spaces.
    To accept more than one answer, separate them with `|`, for example `oak tree|oak`.
- Review photos: approve or reject each one from a gallery of photos waiting for review.
  - A rejected photo can be retaken. You can also reject a photo after approving it, which removes its points.
  - Or turn on **Auto-approve photos** to count every photo as soon as it arrives.
- See a live progress grid of who has found which item (📷 means a photo is waiting for review), plus scores and the time of each player's last find.
- Remove a single player, or **reset** the game to clear all players and keep the items.

## Player tools

- Join with a game code and a name or team name. Names must be unique within a game.
- See each clue, then tap **📷 Take photo** to open the camera, or type an answer.
  - Photos are shrunk on the phone to about 200 KB before upload, so they're quick to send even on weak signal.
  - The typed answers themselves are never sent to the browser.
- Track your score and progress, and see the live leaderboard. Ties go to whoever reached the score first.
- Your session is remembered in the browser, so refreshing the page doesn't lose your progress.

## Offline mode

Hunts often go where the signal doesn't. When a player loses their connection:

- The page keeps showing the clues, their progress and the last leaderboard it received.
- Photos and typed answers are saved on the phone, in the browser's IndexedDB, and marked as waiting.
  They survive closing the page.
- When the connection returns, saved entries are sent automatically, oldest first.
  - A find counts from when the player took the photo or typed the answer, not when it was synced, so being offline doesn't cost tie-breaks.
  - The time a phone reports is limited to between the player joining and the moment it reaches the server.
  - If a typed answer saved offline turns out to be wrong, the player is told and can try again.
- If the organizer ends the game before a phone reconnects, its saved entries stay on the phone.
  Setting the game back to **Active** lets them sync.

**Reloading the page while offline** needs a service worker, and browsers only allow those on **HTTPS** or `localhost`.
On plain `http://<your-ip>` the page still saves and syncs entries as long as it stays open, but a reload with no signal won't load.
For full offline support, serve over HTTPS in one of these ways:

- Pass a certificate the phones trust with `--cert`/`--key`.
- Put the app behind an HTTPS reverse proxy or tunnel, such as Caddy, Cloudflare Tunnel or ngrok.

Players need to load the page once while online before they can use it offline.
Joining a game always needs a connection.

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
  static/       player (index.html, player.js), admin (admin.html, admin.js),
                offline outbox (offline.js) and service worker (sw.js)
tests/          unit + HTTP API tests
```

## Notes and limitations

- Photos are stored as files in the `--photos` folder and are only served to the player who took them and to admins.
  They're deleted when their player, item or game is removed, or when the game is reset.
- An offline find's time comes from the phone, so a player who tampers with requests could backdate an answer
  (never before they joined). This is fine for casual games; photo review limits it for photo items.
- There is one shared admin password. Admin logins are kept in memory, so restarting the server logs admins out. Player sessions are stored in the database and survive a restart.
- The built-in server is meant for small events on a trusted network. To run it on the public internet, put it behind HTTPS (for example, a reverse proxy).
