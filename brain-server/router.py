"""Local inference client and top-level semantic router."""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

from config import Settings
from models import RouteRequest, RouteResponse
from skills import BaseSkill

logger = logging.getLogger("glint.router")


class InferenceClient(Protocol):
    model_name: str

    async def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None = None,
        num_predict: int = 12,
    ) -> str:
        """Send a low-latency chat completion request."""

    async def is_healthy(self) -> bool:
        """Check whether the local inference backend is reachable."""

    async def warmup(self, system_prompt: str) -> None:
        """Preload model weights into memory and prime the KV prefix cache."""

    async def close(self) -> None:
        """Release HTTP connection pool."""


class OllamaClient:
    """Async Ollama client tuned for persistent resident 2-3B routing models."""

    def __init__(self, cfg: Settings) -> None:
        self._cfg = cfg
        self.model_name = cfg.model_name
        self._client = httpx.AsyncClient(
            base_url=cfg.ollama_base_url,
            timeout=httpx.Timeout(cfg.ollama_timeout_s, connect=2.0),
        )

    async def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None = None,
        num_predict: int = 12,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model_name,
            "stream": False,
            "keep_alive": self._cfg.keep_alive,
            "think": self._cfg.enable_thinking,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "options": {
                "temperature": 0.0,
                "num_predict": num_predict,
            },
        }
        if json_schema is not None:
            payload["format"] = json_schema
        resp = await self._client.post("/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return str(data.get("message", {}).get("content", "")).strip()

    async def is_healthy(self) -> bool:
        try:
            resp = await self._client.get("/api/tags", timeout=1.5)
            return resp.status_code == 200
        except Exception:
            return False

    async def warmup(self, system_prompt: str) -> None:
        try:
            await self._client.post(
                "/api/chat",
                json={
                    "model": self.model_name,
                    "stream": False,
                    "keep_alive": self._cfg.keep_alive,
                    "think": self._cfg.enable_thinking,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": "App: System\nText: warmup"},
                    ],
                    "options": {"temperature": 0.0, "num_predict": 1},
                },
                timeout=30.0,
            )
            logger.info("Warmed up Ollama model '%s' (keep_alive=%s)", self.model_name, self._cfg.keep_alive)
        except Exception as exc:
            logger.warning("Warmup skipped or failed for '%s': %s", self.model_name, exc)

    async def close(self) -> None:
        await self._client.aclose()


class SemanticRouter:
    """Dispatches requests to registered micro-skills (Phase 1: cognitive_cards)."""

    def __init__(self, skills: dict[str, BaseSkill], default_skill: str = "cognitive_cards") -> None:
        self._skills = skills
        self._default_skill = default_skill

    async def route(self, request: RouteRequest) -> RouteResponse:
        skill_name = request.skill_hint or self._default_skill
        skill = self._skills.get(skill_name) or self._skills[self._default_skill]
        return await skill.execute(request)
