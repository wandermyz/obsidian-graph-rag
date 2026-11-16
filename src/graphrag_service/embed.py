"""Embedders. The real one is a sentence-transformers model on CPU; `hash` is for tests."""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict

import numpy as np

from .config import QUERY_INSTRUCTION, Settings
from .text import RE_WORD, fts_text


class HashEmbedder:
    """Deterministic bag-of-tokens vectors. No model download; only for tests and smoke runs."""

    def __init__(self, dim: int):
        self.dim, self.model_id = dim, f"hash-{dim}"

    def _one(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for tok in RE_WORD.findall(fts_text(text).casefold()):
            h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:8], "little")
            v[h % self.dim] += 1.0 if (h >> 32) & 1 else -1.0
        n = float(np.linalg.norm(v))
        return v / n if n else v

    def encode_docs(self, texts):
        return np.stack([self._one(t) for t in texts]) if texts else np.zeros((0, self.dim), np.float32)

    def encode_query(self, text):
        return self._one(text)


class STEmbedder:
    def __init__(self, settings: Settings):
        import torch
        from sentence_transformers import SentenceTransformer
        torch.set_num_threads(settings.embed_threads)
        # float32: the checkpoint is bfloat16, which is about 2.5x slower on CPU.
        self.model = SentenceTransformer(settings.model, device="cpu",
                                         model_kwargs={"torch_dtype": torch.float32})
        self.model.max_seq_length = 2048
        self.dim = self.model.get_embedding_dimension()
        self.model_id = settings.model
        self.batch = settings.embed_batch
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()

    def encode_docs(self, texts):
        with self._lock:
            return self.model.encode(texts, batch_size=self.batch, normalize_embeddings=True,
                                     show_progress_bar=False).astype(np.float32)

    def encode_query(self, text):
        hit = self._cache.get(text)
        if hit is not None:
            self._cache.move_to_end(text)
            return hit
        with self._lock:
            v = self.model.encode([text], prompt=f"Instruct: {QUERY_INSTRUCTION}\nQuery:",
                                  normalize_embeddings=True, show_progress_bar=False)[0].astype(np.float32)
        self._cache[text] = v
        if len(self._cache) > 512:
            self._cache.popitem(last=False)
        return v


def make(settings: Settings):
    if settings.model == "hash":
        return HashEmbedder(settings.dim)
    return STEmbedder(settings)
