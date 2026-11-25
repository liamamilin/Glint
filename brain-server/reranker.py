"""Local Apple Silicon (MPS) Cross-Encoder Reranker backed by BAAI/bge-reranker-v2-m3."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

logger = logging.getLogger("glint.reranker")


class RerankerClient(Protocol):
    model_name: str

    async def score(self, query: str, documents: list[str]) -> list[float] | None:
        """Return sigmoid relevance scores in [0, 1] for each document, or None if unavailable."""

    async def warmup(self) -> None:
        """Preload model weights into memory."""


class LocalBgeReranker:
    """In-process FP16 Cross-Encoder reranker using transformers + torch MPS."""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3", max_length: int = 256) -> None:
        self.model_name = model_name
        self._max_length = max_length
        self._tokenizer = None
        self._model = None
        self._device: str = "cpu"
        self._load_failed: bool = False

    def _ensure_loaded_sync(self) -> bool:
        if self._model is not None:
            return True
        if self._load_failed:
            return False
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            device = "mps" if torch.backends.mps.is_available() else "cpu"
            dtype = torch.float16 if device == "mps" else torch.float32
            logger.info("Loading local reranker '%s' on %s (%s)...", self.model_name, device, dtype)
            tok = AutoTokenizer.from_pretrained(self.model_name, local_files_only=True)
            model = AutoModelForSequenceClassification.from_pretrained(
                self.model_name,
                dtype=dtype,
                local_files_only=True,
            ).to(device).eval()
            self._tokenizer = tok
            self._model = model
            self._device = device
            logger.info("Loaded local reranker '%s' on %s", self.model_name, device)
            return True
        except Exception as exc:
            self._load_failed = True
            logger.warning("Local reranker '%s' unavailable (will fall back): %s", self.model_name, exc)
            return False

    def _score_sync(self, query: str, documents: list[str]) -> list[float] | None:
        if not documents or not self._ensure_loaded_sync():
            return None
        import torch

        pairs = [[query, doc] for doc in documents]
        with torch.no_grad():
            inputs = self._tokenizer(  # type: ignore[misc]
                pairs,
                padding=True,
                truncation=True,
                max_length=self._max_length,
                return_tensors="pt",
            ).to(self._device)
            logits = self._model(**inputs).logits.view(-1)  # type: ignore[misc]
            scores = logits.float().cpu().tolist()
            if self._device == "mps":
                torch.mps.synchronize()
        return [float(s) for s in scores]

    async def score(self, query: str, documents: list[str]) -> list[float] | None:
        return await asyncio.to_thread(self._score_sync, query, documents)

    async def warmup(self) -> None:
        await asyncio.to_thread(self._score_sync, "warmup", ["warmup document"])
