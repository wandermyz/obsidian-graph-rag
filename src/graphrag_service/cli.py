"""graphrag serve | status | reembed"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from . import config


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="graphrag", description="GraphRAG service")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="run the HTTP service")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    sub.add_parser("status", help="ask a running service for its status")
    sub.add_parser("reembed", help="drop every vector so the service re-embeds (stop the service first)")
    args = ap.parse_args(argv)
    settings = config.load()

    if args.cmd == "serve":
        import uvicorn
        from .app import create_app
        host = args.host or settings.host
        if host not in ("127.0.0.1", "localhost", "::1"):
            print(f"graphrag: binding to {host} exposes the index beyond this machine", file=sys.stderr)
        uvicorn.run(create_app(settings), host=host, port=args.port or settings.port, log_level="warning")
        return 0

    if args.cmd == "status":
        try:
            with urllib.request.urlopen(f"http://{settings.host}:{settings.port}/status", timeout=10) as resp:
                print(json.dumps(json.load(resp), indent=1, ensure_ascii=False))
        except Exception as exc:
            print(f"graphrag: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "reembed":
        from .store import Store
        store = Store(settings.db_path, settings.dim, settings.model)
        store.reset_embeddings()
        print(f"cleared; {store.pending()} rows will be embedded when the service starts")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
