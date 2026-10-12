import argparse
import os
import sys

from .server import make_server


def main():
    parser = argparse.ArgumentParser(description="Run the scavenger hunt server.")
    parser.add_argument("--host", default=os.environ.get("SCAVENGER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SCAVENGER_PORT", "8000")))
    parser.add_argument("--db", default=os.environ.get("SCAVENGER_DB", "scavenger.db"),
                        help="SQLite database file (default: scavenger.db)")
    args = parser.parse_args()

    password = os.environ.get("SCAVENGER_ADMIN_PASSWORD")
    if not password:
        password = "admin"
        print("WARNING: SCAVENGER_ADMIN_PASSWORD is not set; the admin password is 'admin'.",
              file=sys.stderr)

    server = make_server(args.host, args.port, args.db, password)
    print(f"Players:  http://{args.host}:{args.port}/")
    print(f"Admin:    http://{args.host}:{args.port}/admin")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
