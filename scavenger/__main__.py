import argparse
import os
import ssl
import sys

from .server import make_server
from .store import Store


def env(*names, default=None):
    """First non-empty environment variable among `names`."""
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
    return default


def main():
    # Hosting platforms such as Railway pass the port in $PORT and mount persistent
    # storage at $RAILWAY_VOLUME_MOUNT_PATH; use them unless overridden.
    data_dir = env("RAILWAY_VOLUME_MOUNT_PATH", "SCAVENGER_DATA_DIR", default=".")
    public_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")

    parser = argparse.ArgumentParser(description="Run the scavenger hunt server.")
    parser.add_argument("--host", default=env("SCAVENGER_HOST", default="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(env("SCAVENGER_PORT", "PORT", default="8000")))
    parser.add_argument("--db", default=env("SCAVENGER_DB", default=os.path.join(data_dir, "scavenger.db")),
                        help="SQLite database file (default: scavenger.db)")
    parser.add_argument("--photos", default=env("SCAVENGER_PHOTOS", default=os.path.join(data_dir, "photos")),
                        help="folder for uploaded photos (default: photos)")
    parser.add_argument("--cert", default=os.environ.get("SCAVENGER_CERT"),
                        help="TLS certificate (PEM) to serve HTTPS; needed for offline reloads on phones")
    parser.add_argument("--key", default=os.environ.get("SCAVENGER_KEY"),
                        help="TLS private key (PEM) for --cert")
    parser.add_argument("--list-games", action="store_true",
                        help="print every game's admin link (to recover a lost one) and exit")
    parser.add_argument("--base-url", default=env("SCAVENGER_BASE_URL",
                                                  default=public_domain and f"https://{public_domain}"),
                        help="public address used when printing links, e.g. https://hunt.example.com")
    args = parser.parse_args()
    if bool(args.cert) != bool(args.key):
        parser.error("--cert and --key must be given together")

    scheme = "https" if args.cert else "http"
    base = (args.base_url or f"{scheme}://{args.host}:{args.port}").rstrip("/")

    if args.list_games:
        store = Store(args.db, args.photos)
        games = store.list_games()
        store.close()
        if not games:
            print("No games yet.")
        for g in games:
            print(f"{g['name']}  [{g['status']}]  code {g['code']}")
            print(f"    admin link: {base}/admin#{g['admin_key']}")
        return

    ssl_context = None
    if args.cert:
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(args.cert, args.key)

    if os.environ.get("RAILWAY_ENVIRONMENT") and not os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"):
        print("WARNING: no Railway volume is attached. Games and photos will be LOST on every "
              "redeploy or restart. Attach a volume to this service (any mount path, e.g. /data).",
              file=sys.stderr, flush=True)

    server = make_server(args.host, args.port, args.db, photo_dir=args.photos, ssl_context=ssl_context)
    print(f"Players:          {base}/")
    print(f"Create a game:    {base}/admin")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
