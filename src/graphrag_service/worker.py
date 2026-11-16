"""Background embedding. Ingest returns at once; rows become vector-searchable as this catches up."""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("graphrag.embed")


class EmbedWorker(threading.Thread):
    def __init__(self, store, get_embedder, batch: int = 8):
        super().__init__(daemon=True, name="embed-worker")
        self.store, self.get_embedder, self.batch = store, get_embedder, batch
        self.wake = threading.Event()
        self.stopping = False
        self.last_error: str | None = None

    def run(self):
        while not self.stopping:
            try:
                work = self.store.next_to_embed(self.batch)
                if not work:
                    self.wake.wait(timeout=30)
                    self.wake.clear()
                    continue
                kind, items = work
                vectors = self.get_embedder().encode_docs([text for _, _, text in items])
                self.store.save_embeddings(kind, items, vectors)
                self.last_error = None
            except Exception as exc:  # keep serving; report through /status
                log.exception("embedding failed")
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.wake.wait(timeout=30)
                self.wake.clear()

    def drain(self):
        """Embed everything pending on the calling thread. For tests."""
        while True:
            work = self.store.next_to_embed(self.batch)
            if not work:
                return
            kind, items = work
            self.store.save_embeddings(kind, items, self.get_embedder().encode_docs([t for _, _, t in items]))
