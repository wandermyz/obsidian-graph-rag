"""HTTP API. No authentication of its own: it binds to localhost, and anything reaching it from
elsewhere has to come through an authenticating tunnel (e.g. a dev tunnel)."""
from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import config, embed
from .search import Filters, Searcher
from .store import BadRow, Store, unj
from .worker import EmbedWorker


class FilterBody(BaseModel):
    types: list[str] | None = None
    after: str | None = None
    domain: str | None = None
    markers: list[str] | None = None
    include_raw: bool = True


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    mode: str = Field(default="hybrid", pattern="^(vector|hybrid|graph)$")
    top_k: int = Field(default=8, ge=1, le=50)
    max_tokens: int = Field(default=3000, ge=100, le=30000)
    filters: FilterBody = FilterBody()
    include_superseded: bool = False


class IngestBody(BaseModel):
    upsert: dict[str, list[dict[str, Any]]] = {}
    delete: dict[str, list[str]] = {}
    replace: bool = False                 # true: anything not in `upsert` is deleted
    state: dict[str, Any] | None = None   # the client's own note: revision, export status, backlog


def create_app(settings: config.Settings | None = None, embedder=None) -> FastAPI:
    settings = settings or config.load()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    # The model takes a few seconds to load; do not hold up /healthz for it.
    box: dict[str, Any] = {"embedder": embedder, "error": None}
    ready = threading.Event()

    def load_model():
        try:
            if box["embedder"] is None:
                box["embedder"] = embed.make(settings)
        except Exception as exc:
            box["error"] = f"{type(exc).__name__}: {exc}"
        ready.set()

    def get_embedder():
        ready.wait()
        if box["embedder"] is None:
            raise RuntimeError(f"embedding model failed to load: {box['error']}")
        return box["embedder"]

    model_id = embedder.model_id if embedder is not None else settings.model
    dim = embedder.dim if embedder is not None else settings.dim
    store = Store(settings.db_path, dim, model_id)
    worker = EmbedWorker(store, get_embedder, settings.embed_batch)

    class LazySearcher(Searcher):
        @property
        def embedder(self):
            return get_embedder()

        @embedder.setter
        def embedder(self, _):
            pass

    searcher = LazySearcher(store, None)
    started = time.time()

    @asynccontextmanager
    async def lifespan(_app):
        threading.Thread(target=load_model, daemon=True, name="model-load").start()
        worker.start()
        yield
        worker.stopping = True
        worker.wake.set()

    app = FastAPI(title="GraphRAG service", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.store, app.state.worker, app.state.searcher, app.state.settings = store, worker, searcher, settings
    app.state.load_model = load_model

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/search")
    def search(body: SearchBody):
        fb = body.filters
        f = Filters(types=fb.types, after=fb.after, domain=fb.domain, markers=fb.markers,
                    include_raw=fb.include_raw, include_superseded=body.include_superseded)
        try:
            out = searcher.search(body.query, body.mode, body.top_k, body.max_tokens, f)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc))
        return out

    @app.get("/entities/{name}")
    def entity(name: str, include_superseded: bool = False):
        out = searcher.entity(name, Filters(include_superseded=include_superseded))
        if out is None:
            raise HTTPException(404, f"no entity named '{name}'")
        return out

    @app.get("/entities/{name}/neighbors")
    def neighbors(name: str, predicate: str | None = None, include_superseded: bool = False):
        out = searcher.neighbors(name, predicate, Filters(include_superseded=include_superseded))
        if out is None:
            raise HTTPException(404, f"no entity named '{name}'")
        return {"entity": name, "neighbors": out}

    @app.get("/open")
    def open_items(entity: str | None = None, owner: str | None = None, older_than_days: int | None = None):
        out = searcher.open_items(entity, owner, older_than_days, Filters())
        return {"count": len(out), "open": out}

    @app.get("/documents")
    def document(path: str):
        out = searcher.document(path, Filters())
        if out is None:
            raise HTTPException(404, f"no document at '{path}'")
        return out

    @app.get("/status")
    def status():
        return {
            "index": searcher.index_info(),
            "counts": store.counts(),
            "client_state": unj(store.meta("client_state"), {}),
            "last_ingest_at": store.meta("last_ingest_at"),
            "model": {"id": model_id, "dim": dim, "loaded": ready.is_set() and box["embedder"] is not None,
                      "error": box["error"]},
            "embed_error": worker.last_error,
            "schema": store.meta("schema"),
            "uptime_s": int(time.time() - started),
        }

    @app.get("/ingest/manifest")
    def manifest():
        return {"manifest": store.manifest(), "client_state": unj(store.meta("client_state"), {})}

    @app.post("/ingest")
    def ingest(body: IngestBody):
        try:
            stats = store.apply(body.upsert, body.delete, body.replace, body.state)
        except BadRow as exc:
            raise HTTPException(422, str(exc))
        worker.wake.set()
        return {"stats": stats, "pending_embeddings": store.pending()}

    return app
