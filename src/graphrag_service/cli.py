"""graphrag serve | status | token | reembed"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

from . import config
from .auth import SCOPES, Tokens


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="graphrag", description="GraphRAG service")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="run the HTTP service")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    st = sub.add_parser("status", help="ask a running service for its status")
    st.add_argument("--token", help="a read token (default: $GRAPHRAG_TOKEN)")
    tk = sub.add_parser("token", help="manage bearer tokens")
    tk.add_argument("action", choices=["add", "list", "revoke"])
    tk.add_argument("name", nargs="?")
    tk.add_argument("--scope", action="append", choices=SCOPES)
    tk.add_argument("--domain", help="restrict the token to rows of this domain, e.g. public")
    tk.add_argument("--rate", type=int, help="requests per minute")
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

    if args.cmd == "token":
        tokens = Tokens(settings.tokens_path)
        if args.action == "list":
            for r in tokens.rows():
                print(f"{r['name']:<20} scopes={','.join(r['scopes'])} domain={r.get('domain') or '*'} "
                      f"created={r.get('created')}")
            return 0
        if not args.name:
            ap.error("token add/revoke needs a name")
        if args.action == "revoke":
            print("revoked" if tokens.revoke(args.name) else "no such token")
            return 0
        print(tokens.add(args.name, args.scope or ["read"], args.domain, args.rate))
        print("Shown once. Store it now.", file=sys.stderr)
        return 0

    if args.cmd == "status":
        token = args.token or os.environ.get("GRAPHRAG_TOKEN", "")
        req = urllib.request.Request(f"http://{settings.host}:{settings.port}/status",
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
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
