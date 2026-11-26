"""Unit tests for Glint brain-server v2.0 dual-path LensRouterSkill."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from main import create_app


class FakeDualInferenceClient:
    def __init__(
        self,
        chat_response: str,
        embed_vec: list[float] | None = None,
        fail_chat: bool = False,
        fail_embed: bool = False,
    ) -> None:
        self.model_name = "fake-qwen3.5:2b"
        self._chat_response = chat_response
        self._embed_vec = embed_vec
        self._fail_chat = fail_chat
        self._fail_embed = fail_embed

    async def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None = None,
        num_predict: int = 36,
    ) -> str:
        if self._fail_chat:
            raise RuntimeError("simulated 2B timeout")
        return self._chat_response

    async def embed(self, text: str) -> list[float] | None:
        if self._fail_embed:
            raise RuntimeError("simulated bge-m3 error")
        return self._embed_vec

    async def is_healthy(self) -> bool:
        return not self._fail_chat

    async def warmup(self, system_prompt: str) -> None:
        return None

    async def close(self) -> None:
        return None


def _build_mock_dist(root: Path, version: str = "v1") -> Path:
    rel = root / "releases" / version
    rel.mkdir(parents=True, exist_ok=True)
    lenses = [
        {
            "id": "lens_arch_mece",
            "title": "架构MECE拆解",
            "tagline": "拆解混乱架构边界",
            "routing": {
                "primitive": "deconstruct",
                "goals": ["optimize"],
                "clarity_fit": ["concrete", "vague"],
                "domains": ["architecture"],
                "app_affinity": ["Cursor"],
                "pain_point": "架构耦合太重无从下手",
                "trigger_utterances": ["帮我分析这个架构"],
            },
            "execution": {
                "action_type": "inject_prompt",
                "inject_mode": "append",
                "content": "请按 MECE 拆解架构模块。",
                "variables": ["target_sla"],
            },
            "meta": {"source": "prompt-taxonomy", "base_weight": 1.0, "status": "active"},
        },
        {
            "id": "lens_writing_mece",
            "title": "文章大纲拆解",
            "tagline": "拆解长文写作结构",
            "routing": {
                "primitive": "deconstruct",
                "goals": ["create"],
                "clarity_fit": ["concrete"],
                "domains": ["writing"],
                "app_affinity": ["Notion"],
                "pain_point": "写文章逻辑混乱",
                "trigger_utterances": ["帮我列文章大纲"],
            },
            "execution": {
                "action_type": "inject_prompt",
                "inject_mode": "append",
                "content": "请帮我拆解文章大纲。",
                "variables": [],
            },
            "meta": {"source": "prompt-taxonomy", "base_weight": 1.0, "status": "active"},
        },
        {
            "id": "lens_premortem",
            "title": "事前验尸排雷",
            "tagline": "假设失败反推致命漏洞",
            "routing": {
                "primitive": "critique",
                "goals": ["evaluate", "optimize"],
                "clarity_fit": ["concrete", "conflicted"],
                "domains": ["architecture", "strategy"],
                "app_affinity": ["Cursor", "Notion"],
                "pain_point": "担心方案有隐藏盲区",
                "trigger_utterances": ["帮我排雷"],
            },
            "execution": {
                "action_type": "inject_prompt",
                "inject_mode": "append",
                "content": "请用事前验尸视角找出3个最致命漏洞。",
                "variables": [],
            },
            "meta": {"source": "glint-core", "base_weight": 1.0, "status": "active"},
        },
        {
            "id": "lens_clarify",
            "title": "先反问3个问题",
            "tagline": "模糊态先澄清缺失前提",
            "routing": {
                "primitive": "metacognize",
                "goals": ["create", "optimize"],
                "clarity_fit": ["vague", "conflicted"],
                "domains": ["product"],
                "app_affinity": ["Notion"],
                "pain_point": "想法太散不知道缺什么",
                "trigger_utterances": ["没头绪先问我问题"],
            },
            "execution": {
                "action_type": "show_checklist",
                "inject_mode": None,
                "content": "先向我提出3个关键澄清问题。",
                "variables": [],
            },
            "meta": {"source": "discovery-taxonomy", "base_weight": 1.0, "status": "active"},
        },
    ]
    (rel / "lenses.json").write_text(json.dumps(lenses, ensure_ascii=False), encoding="utf-8")
    (rel / "manifest.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    np.savez_compressed(
        rel / "embeddings.npz",
        ids=np.array([l["id"] for l in lenses]),
        vectors=np.array(
            [
                [1.0, 0.0, 0.0, 0.0],  # lens_arch_mece
                [0.0, 1.0, 0.0, 0.0],  # lens_writing_mece
                [0.8, 0.2, 0.0, 0.0],  # lens_premortem
                [0.5, 0.5, 0.0, 0.0],  # lens_clarify
            ],
            dtype=np.float32,
        ),
    )
    current = root / "current"
    if current.is_symlink() or current.exists():
        current.unlink()
    os.symlink(f"releases/{version}", current)
    return current


def test_v2_dual_path_routes_by_intent_and_embedding(tmp_path: Path) -> None:
    dist_current = _build_mock_dist(tmp_path, "v1")
    fake = FakeDualInferenceClient(
        chat_response=json.dumps({"picks": [1, 2, 3], "primitive": "deconstruct", "goal": "optimize", "clarity": "concrete"}),
        embed_vec=[1.0, 0.0, 0.0, 0.0],  # aligns with lens_arch_mece as candidate #1
    )
    cfg = Settings(
        dist_dir=dist_current,
        db_path=tmp_path / "usage.db",
        warmup_on_startup=False,
    )
    app = create_app(cfg, inference_client=fake)

    with TestClient(app) as client:
        h = client.get("/health").json()
        assert h["router_mode"] == "v2_dual_path"
        assert h["cards_loaded"] == 4
        assert h["release_version"] == "v1"

        resp = client.post(
            "/route",
            json={"app_name": "Cursor", "input_text": "去AI味只给表格，帮我分析这个微服务架构"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["used_fallback"] is False
        assert body["intent"]["primitive"] == "deconstruct"
        assert body["intent"]["used_embed"] is True
        cards = body["cards"]
        assert len(cards) == 3
        # Slot 1 picked architecture lens over writing lens via bge-m3 + App affinity
        assert cards[0]["id"] == "lens_arch_mece"
        # Multi-variable zero-friction instruction automatically appended
        assert "简短反问" in cards[0]["prompt"]
        # All 3 cards have distinct primitives (orthogonal)
        assert len({c["primitive"] for c in cards}) == 3


def test_v2_graceful_fallback_when_both_models_fail(tmp_path: Path) -> None:
    dist_current = _build_mock_dist(tmp_path, "v1")
    fake = FakeDualInferenceClient("", fail_chat=True, fail_embed=True)
    cfg = Settings(
        dist_dir=dist_current,
        db_path=tmp_path / "usage.db",
        warmup_on_startup=False,
    )
    app = create_app(cfg, inference_client=fake)

    with TestClient(app) as client:
        resp = client.post(
            "/route",
            json={"app_name": "Cursor", "input_text": "这个架构太乱了帮我拆一下"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["used_fallback"] is True
        assert body["intent"]["used_embed"] is False
        assert len(body["cards"]) == 3
        assert len({c["primitive"] for c in body["cards"]}) == 3


def test_usage_logger_redacts_secrets_and_stores_v2_intent(tmp_path: Path) -> None:
    import sqlite3

    from logger import UsageLogger
    from models import FeedbackRequest

    db = tmp_path / "usage.db"
    ul = UsageLogger(db)
    ul.log_feedback(
        FeedbackRequest(
            app_name="Cursor",
            input_text="我的key是 sk-abc1234567890abcdef，手机号 13812345678，password=mySecret99",
            recommended_ids=["a", "b", "c"],
            selected_id="a",
            predicted_primitive="deconstruct",
            predicted_goal="optimize",
            predicted_clarity="concrete",
            release_version="20261124_test",
        )
    )
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT input_text, predicted_primitive, predicted_goal, predicted_clarity, release_version FROM usage_logs"
    ).fetchone()
    conn.close()
    assert "sk-abc1234567890abcdef" not in row[0]
    assert "13812345678" not in row[0]
    assert "mySecret99" not in row[0]
    assert "[REDACTED_KEY]" in row[0]
    assert "138****5678" in row[0]
    assert row[1:] == ("deconstruct", "optimize", "concrete", "20261124_test")


def test_v2_workflow_session_chaining_pins_next_step(tmp_path: Path) -> None:
    dist_current = _build_mock_dist(tmp_path, "v1")
    # Attach workflow link: lens_arch_mece -> lens_premortem
    lenses_path = dist_current / "lenses.json"
    lenses = json.loads(lenses_path.read_text(encoding="utf-8"))
    lenses[0]["execution"]["workflow"] = {
        "workflow_id": "arch-to-premortem",
        "step": 1,
        "next_lens_id": "lens_premortem",
    }
    lenses_path.write_text(json.dumps(lenses, ensure_ascii=False), encoding="utf-8")

    fake = FakeDualInferenceClient(
        chat_response=json.dumps({"picks": [1, 2, 3], "primitive": "deconstruct", "goal": "optimize", "clarity": "concrete"}),
        embed_vec=[1.0, 0.0, 0.0, 0.0],
    )
    cfg = Settings(dist_dir=dist_current, db_path=tmp_path / "usage.db", warmup_on_startup=False)
    app = create_app(cfg, inference_client=fake)

    with TestClient(app) as client:
        # User selects Step 1 (lens_arch_mece) in Cursor
        fb = client.post(
            "/feedback",
            json={
                "app_name": "Cursor",
                "input_text": "拆解架构",
                "recommended_ids": ["lens_arch_mece", "lens_premortem", "lens_clarify"],
                "selected_id": "lens_arch_mece",
            },
        )
        assert fb.status_code == 200

        # Next /route in Cursor automatically pins Step 2 (lens_premortem) at Slot 1!
        r2 = client.post("/route", json={"app_name": "Cursor", "input_text": "继续下一步"})
        assert r2.status_code == 200
        cards = r2.json()["cards"]
        assert cards[0]["id"] == "lens_premortem"
        assert cards[0]["is_workflow_next"] is True


class FakeReranker:
    model_name = "fake-bge-reranker"

    def __init__(self, score_by_substr: dict[str, float]) -> None:
        self._scores = score_by_substr

    async def warmup(self) -> None:
        return None

    async def score(self, query: str, documents: list[str]) -> list[float]:
        out: list[float] = []
        for doc in documents:
            s = -2.0
            for k, v in self._scores.items():
                if k in doc:
                    s = v
                    break
            out.append(s)
        return out


def test_v2_l3_breakthrough_gate_and_inspect_recall(tmp_path: Path) -> None:
    dist_current = _build_mock_dist(tmp_path, "v1")
    shadow_lenses = [
        {
            "id": "l3_rust_async",
            "title": "Rust 异步死锁诊断",
            "tagline": "定位 Tokio 运行时死锁",
            "routing": {
                "primitive": "critique",
                "goals": ["debug"],
                "clarity_fit": ["concrete"],
                "domains": ["engineering"],
                "app_affinity": ["Cursor"],
                "pain_point": "Rust 异步任务卡死",
                "trigger_utterances": ["tokio 死锁怎么查"],
            },
            "execution": {"action_type": "inject_prompt", "inject_mode": "append", "content": "请检查 Mutex 跨 await 点。"},
            "meta": {"source": "atlas-methods", "shelf": "off_shelf", "tier": "B", "category": "engineering", "base_weight": 1.0},
            "_manual_off_shelf": False,
        },
        {
            "id": "l3_manual_banned",
            "title": "人工下架测试卡",
            "tagline": "绝不应该穿透诈尸",
            "routing": {
                "primitive": "deconstruct",
                "goals": ["debug"],
                "clarity_fit": ["concrete"],
                "domains": ["engineering"],
                "app_affinity": ["Cursor"],
                "pain_point": "人工下架卡",
                "trigger_utterances": ["tokio 死锁"],
            },
            "execution": {"action_type": "inject_prompt", "inject_mode": "append", "content": "禁止穿透"},
            "meta": {"source": "atlas-methods", "shelf": "off_shelf", "tier": "B", "category": "engineering", "base_weight": 1.0},
            "_manual_off_shelf": True,
        },
    ]
    (dist_current / "l3_shadow_lenses.json").write_text(json.dumps(shadow_lenses, ensure_ascii=False), encoding="utf-8")
    np.savez_compressed(
        dist_current / "l3_shadow_embeddings.npz",
        ids=np.array(["l3_rust_async", "l3_manual_banned"]),
        vectors=np.array([[1.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]], dtype=np.float32),
    )

    fake_inf = FakeDualInferenceClient(chat_response="{}", embed_vec=[1.0, 0.0, 0.0, 0.0])

    # Case 1: L3 score close to L4 (0.5 vs 0.2, delta=0.3 < 1.5) -> L4 wins 100%, NO breakthrough
    close_reranker = FakeReranker({"架构": 0.2, "Rust 异步": 0.5, "人工下架": 9.0})
    app_close = create_app(
        Settings(dist_dir=dist_current, db_path=tmp_path / "u1.db", warmup_on_startup=False),
        inference_client=fake_inf,
        reranker_client=close_reranker,
    )
    with TestClient(app_close) as client:
        r = client.post("/route", json={"app_name": "Cursor", "input_text": "tokio 死锁怎么查"}).json()
        assert all(not c["is_l3_breakthrough"] for c in r["cards"])
        assert "l3_manual_banned" not in [c["id"] for c in r["cards"]]

    # Case 2: L3 overwhelmingly beats L4 (4.5 vs 0.2, delta=4.3 >= 1.5) -> Breakthrough at Slot 1!
    bt_reranker = FakeReranker({"架构": 0.2, "Rust 异步": 4.5, "人工下架": 9.0})
    app_bt = create_app(
        Settings(dist_dir=dist_current, db_path=tmp_path / "u2.db", warmup_on_startup=False),
        inference_client=fake_inf,
        reranker_client=bt_reranker,
    )
    with TestClient(app_bt) as client:
        r = client.post("/route", json={"app_name": "Cursor", "input_text": "tokio 死锁怎么查"}).json()
        assert r["cards"][0]["id"] == "l3_rust_async"
        assert r["cards"][0]["is_l3_breakthrough"] is True
        assert "l3_manual_banned" not in [c["id"] for c in r["cards"]]

        insp = client.post("/admin/api/inspect-recall", json={"query": "tokio 死锁怎么查", "top_k": 10})
        assert insp.status_code == 200
        ibody = insp.json()
        assert ibody["reranker_active"] is True
        assert ibody["would_show_top3"][0]["id"] == "l3_rust_async"
        by_id = {row["id"]: row for row in ibody["ranked_all"]}
        assert by_id["l3_rust_async"]["breakthrough_status"] == "triggered"
        assert by_id["l3_manual_banned"]["breakthrough_status"] == "manual_excluded"


def test_v2_vague_signal_and_quality_gated_orthogonality(tmp_path: Path) -> None:
    from skills.lens_router import _tokenize_zh_en, _vague_signal_score

    # Vague linguistic detection
    assert _vague_signal_score("帮我分析一下") >= 0.8
    assert _vague_signal_score("我写了一段代码但总感觉") >= 0.7
    assert _vague_signal_score("这个方案不够高级，帮我优化一下") >= 0.7
    assert _vague_signal_score("请把这 40 页财务报告按季度提取现金流明细") == 0.0

    # Chinese conversational stop-bigrams are denoised while preserving domain bigrams
    toks = _tokenize_zh_en("帮我排查一下这个接口报错")
    assert "帮我" not in toks and "这个" not in toks
    assert "排查" in toks and "报错" in toks

    # Quality-Gated Orthogonality preserves 3 orthogonal primitives while replacing a -6.0 vertical cliff card
    # with a universal orthogonal card when available.
    dist_current = _build_mock_dist(tmp_path, "v1")
    lenses_path = dist_current / "lenses.json"
    lenses = json.loads(lenses_path.read_text(encoding="utf-8"))
    # Mark lens_clarify as universal evergreen; add a second critique lens (universal) vs vertical lens_premortem
    lenses[3]["meta"]["shelf"] = "evergreen"
    lenses[3]["meta"]["scope"] = "universal"
    lenses.append(
        {
            "id": "lens_univ_compress",
            "title": "通用摘要压榨",
            "tagline": "通用视角压缩重点",
            "routing": {
                "primitive": "compress",
                "goals": ["digest"],
                "clarity_fit": ["concrete", "vague"],
                "domains": ["general"],
                "app_affinity": [],
                "pain_point": "信息太杂需要聚焦重点",
                "trigger_utterances": ["帮我提炼重点"],
            },
            "execution": {"action_type": "inject_prompt", "inject_mode": "append", "content": "提炼重点"},
            "meta": {"source": "glint-core", "shelf": "evergreen", "scope": "universal", "base_weight": 1.0, "status": "active"},
        }
    )
    lenses_path.write_text(json.dumps(lenses, ensure_ascii=False), encoding="utf-8")
    np.savez_compressed(
        dist_current / "embeddings.npz",
        ids=np.array([l["id"] for l in lenses]),
        vectors=np.array(
            [
                [1.0, 0.0, 0.0, 0.0],
                [0.0, 1.0, 0.0, 0.0],
                [0.8, 0.2, 0.0, 0.0],
                [0.5, 0.5, 0.0, 0.0],
                [0.6, 0.4, 0.0, 0.0],
            ],
            dtype=np.float32,
        ),
    )

    fake_inf = FakeDualInferenceClient(chat_response="{}", embed_vec=[1.0, 0.0, 0.0, 0.0])
    # Slot 1: lens_arch_mece (+2.0, deconstruct)
    # Slot 2: lens_clarify (-2.5, metacognize - solid orthogonal)
    # Slot 3 candidates: lens_premortem (-5.5, vertical critique cliff!) vs lens_univ_compress (-6.0, universal compress)
    # Quality-Gated Orthogonality replaces vertical cliff lens_premortem with universal orthogonal lens_univ_compress!
    cliff_reranker = FakeReranker({"架构MECE": 2.0, "文章大纲": -1.0, "事前验尸": -5.5, "先反问": -2.5, "通用摘要": -6.0})
    app = create_app(
        Settings(dist_dir=dist_current, db_path=tmp_path / "u_cliff.db", warmup_on_startup=False),
        inference_client=fake_inf,
        reranker_client=cliff_reranker,
    )
    with TestClient(app) as client:
        r = client.post("/route", json={"app_name": "Cursor", "input_text": "拆解这个微服务架构"}).json()
        got_ids = [c["id"] for c in r["cards"]]
        assert got_ids[0] == "lens_arch_mece"
        assert "lens_clarify" in got_ids
        assert "lens_univ_compress" in got_ids
        assert "lens_premortem" not in got_ids
        # Strictly 3 distinct primitives preserved
        assert len({c["primitive"] for c in r["cards"]}) == 3
