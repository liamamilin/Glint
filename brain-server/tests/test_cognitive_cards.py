"""Unit tests for Glint brain-server routing, orthogonality, hot-reload, and logging."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from main import create_app


class FakeInferenceClient:
    def __init__(self, response: str, fail: bool = False) -> None:
        self.model_name = "fake-2b"
        self._response = response
        self._fail = fail
        self.calls: list[dict[str, Any]] = []

    async def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None = None,
        num_predict: int = 12,
    ) -> str:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "num_predict": num_predict,
            }
        )
        if self._fail:
            raise RuntimeError("simulated ollama error")
        return self._response

    async def is_healthy(self) -> bool:
        return not self._fail

    async def warmup(self, system_prompt: str) -> None:
        return None

    async def close(self) -> None:
        return None


def _make_settings(tmp_path: Path) -> Settings:
    src_cards = Path(__file__).resolve().parents[1] / "data" / "cards.yaml"
    cards_copy = tmp_path / "cards.yaml"
    cards_copy.write_text(src_cards.read_text(encoding="utf-8"), encoding="utf-8")
    return Settings(
        cards_path=cards_copy,
        db_path=tmp_path / "usage.db",
        warmup_on_startup=False,
    )


def test_route_parses_compact_numeric_indices(tmp_path: Path) -> None:
    fake = FakeInferenceClient("3,6,8")
    app = create_app(_make_settings(tmp_path), inference_client=fake)

    with TestClient(app) as client:
        resp = client.post(
            "/route",
            json={"app_name": "Mail", "input_text": "帮我改一下这封延期邮件"},
        )
        assert resp.status_code == 200
        ids = [c["id"] for c in resp.json()["cards"]]
        assert ids == ["de_ai_rewrite", "decision_matrix", "root_cause"]


def test_route_enforces_orthogonal_dimensions(tmp_path: Path) -> None:
    # Model returns two 'critique' cards (premortem + red_team) and one invalid ID
    fake = FakeInferenceClient(
        json.dumps({"card_ids": ["premortem", "red_team", "nonexistent_card"]})
    )
    app = create_app(_make_settings(tmp_path), inference_client=fake)

    with TestClient(app) as client:
        resp = client.post(
            "/route",
            json={"app_name": "Obsidian", "input_text": "我想做一个本地AI路由插件"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["skill_type"] == "cognitive_cards"
        assert body["used_fallback"] is False
        cards = body["cards"]
        assert len(cards) == 3
        assert cards[0]["id"] == "premortem"
        # All 3 dimensions must be distinct (red_team dropped because critique already chosen)
        dims = [c["dimension"] for c in cards]
        assert len(set(dims)) == 3


def test_route_fallback_on_inference_failure(tmp_path: Path) -> None:
    fake = FakeInferenceClient("", fail=True)
    app = create_app(_make_settings(tmp_path), inference_client=fake)

    with TestClient(app) as client:
        resp = client.post(
            "/route",
            json={"app_name": "Cursor", "input_text": "这段并发代码偶尔报错 race condition"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["used_fallback"] is True
        cards = body["cards"]
        assert len(cards) == 3
        assert cards[0]["dimension"] == "engineering"
        assert len({c["dimension"] for c in cards}) == 3


def test_hot_reload_cards_yaml(tmp_path: Path) -> None:
    cfg = _make_settings(tmp_path)
    fake = FakeInferenceClient(json.dumps({"card_ids": ["premortem", "clarify_first", "mece_breakdown"]}))
    app = create_app(cfg, inference_client=fake)

    with TestClient(app) as client:
        assert len(client.get("/cards").json()) == 8

        # Append a new card and bump mtime
        updated = cfg.cards_path.read_text(encoding="utf-8") + (
            '\n  - id: "first_principles"\n'
            '    title: "第一性原理"\n'
            '    dimension: "mental_model"\n'
            '    intent: "回归本质"\n'
            '    mode: "append"\n'
            '    prompt: "\\n\\n请用第一性原理拆解核心假设。"\n'
        )
        cfg.cards_path.write_text(updated, encoding="utf-8")
        stat = cfg.cards_path.stat()
        os.utime(cfg.cards_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

        cards = client.get("/cards").json()
        assert len(cards) == 9
        assert any(c["id"] == "first_principles" for c in cards)


def test_feedback_persists_positive_and_negative_samples(tmp_path: Path) -> None:
    cfg = _make_settings(tmp_path)
    fake = FakeInferenceClient(json.dumps({"card_ids": ["premortem", "clarify_first", "mece_breakdown"]}))
    app = create_app(cfg, inference_client=fake)

    with TestClient(app) as client:
        r1 = client.post(
            "/feedback",
            json={
                "app_name": "Google Chrome",
                "input_text": "帮我看看这个产品方案",
                "recommended_ids": ["premortem", "clarify_first", "mece_breakdown"],
                "selected_id": "premortem",
                "inject_mode": "append",
                "latency_ms": 112.5,
            },
        )
        assert r1.status_code == 200
        assert r1.json()["record_id"] == 1

        # Negative sample (user pressed Esc -> selected_id is None)
        r2 = client.post(
            "/feedback",
            json={
                "app_name": "Obsidian",
                "input_text": "随便记两句",
                "recommended_ids": ["premortem", "clarify_first", "mece_breakdown"],
                "selected_id": None,
                "inject_mode": None,
                "latency_ms": 98.2,
            },
        )
        assert r2.status_code == 200
        assert r2.json()["record_id"] == 2

        # Custom intent sample (4th option: user wrote what they actually wanted)
        r3 = client.post(
            "/feedback",
            json={
                "app_name": "备忘录",
                "input_text": "帮我总结这一段文字",
                "recommended_ids": ["de_ai_rewrite", "mece_breakdown", "red_team"],
                "selected_id": "__custom__",
                "inject_mode": "append",
                "latency_ms": 210.4,
                "custom_intent": "请提炼出 3 条核心要点，每条不超过 20 字",
            },
        )
        assert r3.status_code == 200
        assert r3.json()["record_id"] == 3
