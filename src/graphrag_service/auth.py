"""Bearer tokens. Only SHA-256 digests are stored; a token is shown once, when it is created."""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

SCOPES = ("read", "ingest")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Tokens:
    def __init__(self, path: Path):
        self.path = path
        self._mtime, self._rows = None, []
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def rows(self) -> list[dict]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return []
        if mtime != self._mtime:
            self._rows = json.loads(self.path.read_text(encoding="utf-8"))
            self._mtime = mtime
        return self._rows

    def _save(self, rows):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        self._mtime = None

    def add(self, name: str, scopes: list[str], domain: str | None = None, rate: int | None = None) -> str:
        rows = [r for r in self.rows() if r["name"] != name]
        token = "grag_" + secrets.token_urlsafe(32)
        rows.append({"name": name, "sha256": digest(token), "scopes": scopes, "domain": domain, "rate": rate,
                     "created": time.strftime("%Y-%m-%d")})
        self._save(rows)
        return token

    def revoke(self, name: str) -> bool:
        rows = self.rows()
        keep = [r for r in rows if r["name"] != name]
        self._save(keep)
        return len(keep) != len(rows)

    def find(self, token: str) -> dict | None:
        d = digest(token)
        for r in self.rows():
            if secrets.compare_digest(r["sha256"], d):
                return r
        return None

    def allow(self, name: str, per_minute: int) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[name]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= per_minute:
                return False
            q.append(now)
            return True
