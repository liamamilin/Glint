"""Phase 1 Micro-Skill: Cognitive Cards (认知提词卡片路由)."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import yaml

from models import CardItem, RouteRequest, RouteResponse
from router import InferenceClient
from skills import BaseSkill

logger = logging.getLogger("glint.skills.cognitive_cards")

CARD_IDS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "card_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 3,
        }
    },
    "required": ["card_ids"],
}


def _build_preview(prompt: str, max_chars: int = 44) -> str:
    compact = re.sub(r"\s+", " ", prompt.replace("{{input}}", "").strip())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"


class CognitiveCardsSkill(BaseSkill):
    """Routes partial user text + active app context to 3 orthogonal cognitive cards."""

    name = "cognitive_cards"

    def __init__(
        self,
        *,
        cards_path: Path,
        inference: InferenceClient,
        top_k: int = 3,
        max_input_chars: int = 1200,
    ) -> None:
        self._cards_path = cards_path
        self._inference = inference
        self._top_k = top_k
        self._max_input_chars = max_input_chars

        self._cards: list[CardItem] = []
        self._cards_by_id: dict[str, CardItem] = {}
        self._cards_mtime_ns: int = -1
        self._system_prompt: str = ""

        self.reload_if_changed()

    @property
    def cards(self) -> list[CardItem]:
        self.reload_if_changed()
        return list(self._cards)

    @property
    def system_prompt(self) -> str:
        self.reload_if_changed()
        return self._system_prompt

    def reload_if_changed(self) -> bool:
        """Hot-reload cards.yaml whenever the file modification timestamp changes."""
        stat = self._cards_path.stat()
        if stat.st_mtime_ns == self._cards_mtime_ns and self._cards:
            return False

        raw = yaml.safe_load(self._cards_path.read_text(encoding="utf-8")) or {}
        raw_cards = raw.get("cards", [])
        parsed: list[CardItem] = []
        for entry in raw_cards:
            prompt_text = str(entry["prompt"])
            parsed.append(
                CardItem(
                    id=str(entry["id"]),
                    title=str(entry["title"]),
                    dimension=str(entry["dimension"]),
                    intent=str(entry["intent"]),
                    mode=entry.get("mode", "append"),
                    prompt=prompt_text,
                    preview=_build_preview(prompt_text),
                )
            )

        self._cards = parsed
        self._cards_by_id = {card.id: card for card in parsed}
        self._cards_mtime_ns = stat.st_mtime_ns
        self._system_prompt = self._render_system_prompt(parsed)
        logger.info("Loaded %d cognitive cards from %s", len(parsed), self._cards_path)
        return True

    @staticmethod
    def _render_system_prompt(cards: list[CardItem]) -> str:
        """Keep the system prompt ultra-compact and byte-identical for low prefill latency."""
        lines = [
            "根据App和输入内容，从下方选3个不同维度的卡片编号，按优先级仅输出3个数字(逗号分隔，如: 1,4,2)，禁止解释。",
        ]
        for idx, card in enumerate(cards, start=1):
            lines.append(f"{idx}|{card.dimension}|{card.title}|{card.intent}")
        return "\n".join(lines)

    async def execute(self, request: RouteRequest) -> RouteResponse:
        self.reload_if_changed()
        start = time.perf_counter()

        trimmed_text = request.input_text.strip()[: self._max_input_chars]
        user_prompt = f"App: {request.app_name}\nText: {trimmed_text}"

        raw_ids: list[str] = []
        used_fallback = False
        try:
            content = await self._inference.chat_json(
                system_prompt=self._system_prompt,
                user_prompt=user_prompt,
                json_schema=None,
                num_predict=12,
            )
            raw_ids = self._parse_card_ids(content, self._cards)
            if not raw_ids:
                used_fallback = True
        except Exception as exc:
            logger.warning("Inference failed, falling back to heuristic selection: %s", exc)
            used_fallback = True

        selected = self.select_orthogonal_cards(
            raw_ids=raw_ids,
            cards=self._cards,
            app_name=request.app_name,
            input_text=trimmed_text,
            top_k=self._top_k,
        )
        latency_ms = round((time.perf_counter() - start) * 1000.0, 2)
        return RouteResponse(
            skill_type=self.name,
            cards=selected,
            latency_ms=latency_ms,
            model=self._inference.model_name,
            used_fallback=used_fallback,
        )

    @staticmethod
    def _parse_card_ids(raw_content: str, cards: list[CardItem]) -> list[str]:
        if not raw_content:
            return []

        # 1) Compatibility path: JSON {"card_ids": [...]}
        if "{" in raw_content:
            match = re.search(r"\{.*\}", raw_content, re.DOTALL)
            if match:
                try:
                    payload = json.loads(match.group(0))
                    ids = payload.get("card_ids")
                    if isinstance(ids, list):
                        return [str(item).strip() for item in ids if isinstance(item, str) and item.strip()]
                except json.JSONDecodeError:
                    pass

        # 2) Fast path: 1-based numeric indices like "1,4,2"
        nums = re.findall(r"\d+", raw_content)
        resolved: list[str] = []
        for token in nums:
            idx = int(token)
            if 1 <= idx <= len(cards):
                resolved.append(cards[idx - 1].id)
        return resolved

    @classmethod
    def select_orthogonal_cards(
        cls,
        *,
        raw_ids: list[str],
        cards: list[CardItem],
        app_name: str,
        input_text: str,
        top_k: int = 3,
    ) -> list[CardItem]:
        """Guarantee `top_k` valid cards with distinct dimensions whenever possible."""
        by_id = {c.id: c for c in cards}
        chosen: list[CardItem] = []
        used_ids: set[str] = set()
        used_dims: set[str] = set()

        # Pass 1: honor model ranking while strictly enforcing dimension orthogonality
        for cid in raw_ids:
            card = by_id.get(cid)
            if card is None or card.id in used_ids or card.dimension in used_dims:
                continue
            chosen.append(card)
            used_ids.add(card.id)
            used_dims.add(card.dimension)
            if len(chosen) >= top_k:
                return chosen

        # Pass 2: backfill from uncovered dimensions using lightweight context hints
        preferred_order = cls._heuristic_order(cards=cards, app_name=app_name, input_text=input_text)
        for card in preferred_order:
            if card.id in used_ids or card.dimension in used_dims:
                continue
            chosen.append(card)
            used_ids.add(card.id)
            used_dims.add(card.dimension)
            if len(chosen) >= top_k:
                return chosen

        # Pass 3: if catalog has fewer than top_k distinct dimensions, fill remaining slots
        for card in preferred_order:
            if card.id in used_ids:
                continue
            chosen.append(card)
            used_ids.add(card.id)
            if len(chosen) >= top_k:
                return chosen

        return chosen

    @staticmethod
    def _heuristic_order(cards: list[CardItem], app_name: str, input_text: str) -> list[CardItem]:
        lower_app = app_name.lower()
        lower_text = input_text.lower()
        is_code_context = any(
            kw in lower_app for kw in ("cursor", "code", "xcode", "terminal", "iterm", "warp", "ghostty")
        ) or any(kw in lower_text for kw in ("error", "exception", "traceback", "bug", "报错", "代码", "函数"))

        if not is_code_context:
            return cards

        return sorted(cards, key=lambda c: (0 if c.dimension == "engineering" else 1))
