import argparse
import os
import ssl
import sys

from .server import make_server


def main():
    parser = argparse.ArgumentParser(description="Run the scavenger hunt server.")
    parser.add_argument("--host", default=os.environ.get("SCAVENGER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SCAVENGER_PORT", "8000")))
    parser.add_argument("--db", default=os.environ.get("SCAVENGER_DB", "scavenger.db"),
                        help="SQLite database file (default: scavenger.db)")
    parser.add_argument("--photos", default=os.environ.get("SCAVENGER_PHOTOS", "photos"),
                        help="folder for uploaded photos (default: photos)")
    parser.add_argument("--cert", default=os.environ.get("SCAVENGER_CERT"),
                        help="TLS certificate (PEM) to serve HTTPS; needed for offline reloads on phones")
    parser.add_argument("--key", default=os.environ.get("SCAVENGER_KEY"),
                        help="TLS private key (PEM) for --cert")
    args = parser.parse_args()
    if bool(args.cert) != bool(args.key):
        parser.error("--cert and --key must be given together")

    password = os.environ.get("SCAVENGER_ADMIN_PASSWORD")
    if not password:
        password = "admin"
        print("WARNING: SCAVENGER_ADMIN_PASSWORD is not set; the admin password is 'admin'.",
              file=sys.stderr)

    ssl_context = None
    if args.cert:
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(args.cert, args.key)

    server = make_server(args.host, args.port, args.db, password,
                         photo_dir=args.photos, ssl_context=ssl_context)
    scheme = "https" if ssl_context else "http"
    print(f"Players:  {scheme}://{args.host}:{args.port}/")
    print(f"Admin:    {scheme}://{args.host}:{args.port}/admin")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
