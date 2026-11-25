"""Local Ollama embedding client (default: bge-m3:latest) for semantic dedup & index build."""

from __future__ import annotations

import os
from typing import Protocol

import httpx
import numpy as np

DEFAULT_OLLAMA_URL = os.getenv("GLINT_OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULT_EMBED_MODEL = os.getenv("GLINT_EMBED_MODEL", "bge-m3:latest")


class Embedder(Protocol):
    model: str

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """Return an L2-normalized float32 matrix of shape (N, D)."""


def l2_normalize(matrix: np.ndarray) -> np.ndarray:
    arr = np.asarray(matrix, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms == 0.0, 1.0, norms)
    return arr / norms


class OllamaEmbedClient:
    """Calls local Ollama /api/embed with batching and L2 normalization."""

    def __init__(
        self,
        model: str = DEFAULT_EMBED_MODEL,
        base_url: str = DEFAULT_OLLAMA_URL,
        timeout_s: float = 60.0,
        batch_size: int = 16,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.batch_size = batch_size

    def is_available(self) -> bool:
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=2.0)
            return resp.status_code == 200
        except Exception:
            return False

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)

        all_vecs: list[list[float]] = []
        with httpx.Client(base_url=self.base_url, timeout=self.timeout_s) as client:
            for i in range(0, len(texts), self.batch_size):
                batch = texts[i : i + self.batch_size]
                resp = client.post(
                    "/api/embed",
                    json={
                        "model": self.model,
                        "input": batch,
                        "keep_alive": -1,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                embeddings = data.get("embeddings")
                if not isinstance(embeddings, list) or len(embeddings) != len(batch):
                    raise RuntimeError(f"Unexpected /api/embed response from Ollama: {str(data)[:200]}")
                all_vecs.extend(embeddings)

        return l2_normalize(np.asarray(all_vecs, dtype=np.float32))
