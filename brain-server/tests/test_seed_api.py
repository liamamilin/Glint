"""Unit tests for Glint Seed probabilistic sampling engine and /seed/* HTTP API endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from config import Settings
from main import create_app
from router import InferenceClient
from seed_store import SeedStore


class _DummyInference(InferenceClient):
    model_name = "dummy"

    async def is_healthy(self) -> bool:
        return True

    async def warmup(self, system_prompt: str) -> None:
        return None

    async def generate_json(self, system_prompt: str, user_prompt: str, schema: dict) -> dict:
        return {}

    async def embed(self, text: str) -> list[float]:
        return []

    async def close(self) -> None:
        return None


def _fake_lens(lid: str, tier: str = "B", category: str = "general") -> dict:
    return {
        "id": lid,
        "title": f"Title {lid}",
        "tagline": f"Tagline {lid}",
        "routing": {"primitive": "deconstruct", "pain_point": "pain"},
        "execution": {"content": "prompt"},
        "meta": {"tier": tier, "category": category, "source": "test"},
    }


def test_seed_weighted_scoring_tier_l3_recall_and_behavior(tmp_path: Path) -> None:
    store = SeedStore(tmp_path / "seed_state.json")
    l4 = [
        _fake_lens("s_card", tier="S", category="thinking"),
        _fake_lens("a_card", tier="A", category="thinking"),
        _fake_lens("b_card", tier="B", category="engineering"),
        _fake_lens("b_recalled", tier="B", category="engineering"),
    ]
    l3 = [
        _fake_lens("l3_1", tier="A", category="cognition"),
        _fake_lens("l3_2", tier="A", category="cognition"),
    ]

    # 1. Tier S > A > B, recall decays weight, L3 total share ~ 15% in smart_mix
    scored, _, _ = store.score_candidates(
        l4_lenses=l4,
        l3_lenses=l3,
        recall_counts={"b_recalled": 5},
        mode="smart_mix",
    )
    by_id = {x["id"]: x for x in scored}
    assert by_id["s_card"]["weight"] > by_id["a_card"]["weight"] > by_id["b_card"]["weight"]
    assert by_id["b_recalled"]["weight"] < by_id["b_card"]["weight"]

    l3_pct = sum(x["probability_pct"] for x in scored if x["layer"] == "L3")
    assert 14.0 <= l3_pct <= 16.0

    # 2. Skip lowers weight; expand/copy boosts category affinity & clears skip
    store.apply_action("a_card", "skip")
    store.apply_action("a_card", "skip")
    scored2, _, _ = store.score_candidates(l4, l3, recall_counts={}, mode="l4_all")
    w_a_skipped = {x["id"]: x["weight"] for x in scored2}["a_card"]
    assert w_a_skipped < by_id["a_card"]["weight"]

    store.apply_action("a_card", "copy", category="thinking")
    scored3, _, _ = store.score_candidates(l4, l3, recall_counts={}, mode="l4_all")
    w_a_copied = {x["id"]: x["weight"] for x in scored3}["a_card"]
    assert w_a_copied > w_a_skipped


def test_seed_endpoints_modes_draw_and_mute_isolation(tmp_path: Path) -> None:
    cfg = Settings(
        db_path=tmp_path / "usage.db",
        seed_state_path=tmp_path / "seed_state.json",
        warmup_on_startup=False,
        enable_reranker=False,
    )
    app = create_app(cfg=cfg, inference_client=_DummyInference())
    client = TestClient(app)

    # smart_mix includes both L4 and L3
    r_mix = client.get("/seed/pool?mode=smart_mix")
    assert r_mix.status_code == 200
    mix_body = r_mix.json()
    assert mix_body["total_l4"] > 0
    assert mix_body["total_l3"] > 0
    layers = {x["layer"] for x in mix_body["items"]}
    assert layers == {"L4", "L3"}

    # POST /seed/next returns a weighted draw with probability_pct & reason_tag
    r_draw = client.post("/seed/next", json={"mode": "smart_mix", "exclude_ids": []})
    assert r_draw.status_code == 200
    drawn = r_draw.json()["item"]
    assert drawn is not None
    assert drawn["probability_pct"] > 0
    assert drawn["reason_tag"]

    # l3_discovery mode only draws L3
    r_l3 = client.post("/seed/next", json={"mode": "l3_discovery", "exclude_ids": []})
    assert r_l3.status_code == 200
    assert r_l3.json()["item"]["layer"] == "L3"

    # Mute an L4 item -> excluded from Seed pool, but still present in /cards active recall
    l4_id = next(x["id"] for x in mix_body["items"] if x["layer"] == "L4")
    r_mute = client.post("/seed/feedback", json={"id": l4_id, "action": "mute"})
    assert r_mute.status_code == 200
    assert r_mute.json()["muted_count"] == 1

    r_l4_pool = client.get("/seed/pool?mode=l4_all")
    assert l4_id not in {x["id"] for x in r_l4_pool.json()["items"]}
    assert l4_id in {c["id"] for c in client.get("/cards").json()}

    # Reset restores everything
    r_reset = client.post("/seed/reset")
    assert r_reset.status_code == 200
    assert r_reset.json()["muted_count"] == 0

    # Config update + state inspection
    r_cfg = client.put(
        "/seed/config",
        json={
            "tier_s_weight": 6.0,
            "tier_a_weight": 2.0,
            "tier_b_weight": 1.0,
            "l3_smart_mix_share": 0.25,
            "recall_decay_rate": 0.5,
            "skip_decay_factor": 0.7,
        },
    )
    assert r_cfg.status_code == 200
    assert r_cfg.json()["l3_smart_mix_share"] == 0.25

    r_state = client.get("/seed/state")
    assert r_state.status_code == 200
    assert r_state.json()["config"]["tier_s_weight"] == 6.0


def test_seed_pinned_focus_mode_and_smart_mix_budget(tmp_path: Path) -> None:
    store = SeedStore(tmp_path / "seed_state.json")
    l4 = [_fake_lens(f"l4_{i}", tier="A") for i in range(6)]
    l3 = [_fake_lens(f"l3_{i}", tier="A") for i in range(3)]

    # Pin 2 cards (1 from L4, 1 from L3)
    pinned = store.set_pinned(["l4_0", "l3_0"], mode="add")
    assert pinned == ["l4_0", "l3_0"]

    # 1. smart_mix allocates ~30% probability budget to pinned cards & bypasses recall decay on pinned cards
    scored_mix, _, _ = store.score_candidates(l4, l3, recall_counts={"l4_0": 20}, mode="smart_mix")
    pinned_pct = sum(x["probability_pct"] for x in scored_mix if x["is_pinned"])
    assert 29.0 <= pinned_pct <= 31.0
    by_id = {x["id"]: x for x in scored_mix}
    assert by_id["l4_0"]["is_pinned"] is True
    assert "精读" in by_id["l4_0"]["reason_tag"]

    # 2. pinned_only mode strictly restricts pool to pinned_ids and cycles smoothly with adaptive cooldown
    scored_only, _, _ = store.score_candidates(l4, l3, recall_counts={"l4_0": 20}, mode="pinned_only")
    assert {x["id"] for x in scored_only} == {"l4_0", "l3_0"}
    assert scored_only[0]["weight"] == scored_only[1]["weight"]

    # 3. Resetting muted/snoozed hidden items preserves user's pinned_ids
    store.apply_action("l4_1", "mute")
    store.reset_all()
    assert store.get_pinned_ids() == ["l4_0", "l3_0"]

    # 4. API endpoints: PUT /seed/pinned & pin/unpin feedback
    cfg = Settings(
        db_path=tmp_path / "usage.db",
        seed_state_path=tmp_path / "api_seed_state.json",
        warmup_on_startup=False,
        enable_reranker=False,
    )
    app = create_app(cfg=cfg, inference_client=_DummyInference())
    client = TestClient(app)

    pool = client.get("/seed/pool?mode=l4_all").json()["items"]
    sample_ids = [pool[0]["id"], pool[1]["id"]]

    r_put = client.put("/seed/pinned", json={"ids": sample_ids, "mode": "replace"})
    assert r_put.status_code == 200
    assert r_put.json()["pinned_count"] == 2

    r_only = client.get("/seed/pool?mode=pinned_only")
    assert r_only.status_code == 200
    assert {x["id"] for x in r_only.json()["items"]} == set(sample_ids)

    r_unpin = client.post("/seed/feedback", json={"id": sample_ids[0], "action": "unpin"})
    assert r_unpin.status_code == 200
    assert r_unpin.json()["pinned_count"] == 1

    r_st = client.get("/seed/state")
    assert r_st.status_code == 200
    assert r_st.json()["pinned_ids"] == [sample_ids[1]]
    assert len(r_st.json()["pinned_items"]) == 1

    # 5. Regression: pinned_smart_mix_share=0.0 keeps natural weight (never zeroes out pinned cards)
    store.update_config({"pinned_smart_mix_share": 0.0})
    scored_zero, _, _ = store.score_candidates(l4, l3, recall_counts={}, mode="smart_mix")
    pinned_zero = [x for x in scored_zero if x["is_pinned"]]
    assert all(x["weight"] > 0 and x["probability_pct"] > 0 for x in pinned_zero)

    # 6. Regression: mute -> unmute (undo) preserves pinned state without client/server drift
    store.apply_action("l4_0", "mute")
    scored_muted, _, _ = store.score_candidates(l4, l3, recall_counts={}, mode="pinned_only")
    assert "l4_0" not in {x["id"] for x in scored_muted}
    store.apply_action("l4_0", "unmute")
    scored_restored, _, _ = store.score_candidates(l4, l3, recall_counts={}, mode="pinned_only")
    assert "l4_0" in {x["id"] for x in scored_restored}
