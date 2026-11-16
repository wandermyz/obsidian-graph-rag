"""Settings, all from the environment. The service has no idea where its data comes from."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"
QUERY_INSTRUCTION = "Given a question, retrieve the facts and passages that answer it"


def _default_data_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8787
    model: str = DEFAULT_MODEL          # "hash" selects the dependency-free test embedder
    dim: int = 1024
    embed_batch: int = 8
    embed_threads: int = 8

    @property
    def db_path(self) -> Path:
        return self.data_dir / "graph.db"


def load() -> Settings:
    env = os.environ.get
    model = env("GRAPHRAG_MODEL", DEFAULT_MODEL)
    return Settings(
        data_dir=Path(env("GRAPHRAG_DATA_DIR") or _default_data_dir()),
        host=env("GRAPHRAG_HOST", "127.0.0.1"),
        port=int(env("GRAPHRAG_PORT", "8787")),
        model=model,
        dim=int(env("GRAPHRAG_DIM", "64" if model == "hash" else "1024")),
        embed_batch=int(env("GRAPHRAG_EMBED_BATCH", "8")),
        embed_threads=int(env("GRAPHRAG_EMBED_THREADS", "8")),
    )
