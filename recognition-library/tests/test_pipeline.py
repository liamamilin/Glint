"""Unit tests for Glint's three-layer cognitive supply chain and all 4 quality gates."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from pipeline.adapters.l1_sources import load_glint_core_l1
from pipeline.builder import build_release, rollback_release
from pipeline.embed_client import l2_normalize
from pipeline.l0_to_l1 import run_l0_to_l1
from pipeline.l1_to_l2 import compile_l1_to_l2, load_existing_l2_lenses
from pipeline.ontology import LIB_ROOT, Ontology
from pipeline.opencode_client import (
    ContentBlockedOpenCodeError,
    FatalOpenCodeError,
    OpenCodeResponse,
    TransientOpenCodeError,
    classify_provider_errors,
    extract_json_object,
    parse_opencode_jsonl,
)
from pipeline.schema import L0RawItem, L1Material, content_sha256


class FakeLLM:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.model = "opencode-go/fake-test"
        self._responses = list(responses)
        self.calls = 0

    def call_json(self, prompt: str) -> OpenCodeResponse:
        self.calls += 1
        payload = self._responses.pop(0)
        return OpenCodeResponse(
            text=json.dumps(payload, ensure_ascii=False),
            parsed_json=payload,
            model=self.model,
            elapsed_s=0.01,
            raw_events=[],
        )


class FakeEmbedder:
    def __init__(self, vectors_by_keyword: dict[str, list[float]]) -> None:
        self.model = "fake-bge-m3"
        self._map = vectors_by_keyword

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        rows: list[list[float]] = []
        for t in texts:
            vec = [0.1, 0.2, 0.3, 0.4]
            for kw, v in self._map.items():
                if kw in t:
                    vec = v
                    break
            rows.append(vec)
        return l2_normalize(np.asarray(rows, dtype=np.float32))


def test_ontology_and_alias_normalization() -> None:
    ont = Ontology.load()
    assert len(ont.primitives) == 7
    assert ont.normalize_primitive("structure") == "deconstruct"
    assert ont.normalize_primitive("REVIEW") == "critique"
    assert ont.normalize_primitive("polish") == "translate"
    assert ont.normalize_primitive("nonexistent_op") is None

    goals, unknown = ont.normalize_list("goals", ["improve", "assess", "bogus"], ont.goals)
    assert goals == ["optimize", "evaluate"]
    assert unknown == ["bogus"]


def test_opencode_jsonl_parsing_and_error_classification() -> None:
    sample_jsonl = "\n".join(
        [
            json.dumps({"type": "text", "part": {"text": "```json\n{\"ok\": true}\n```"}}),
            json.dumps({"type": "step_finish"}),
        ]
    )
    texts, errors, events = parse_opencode_jsonl(sample_jsonl)
    assert not errors
    assert len(events) == 2
    assert extract_json_object("".join(texts)) == {"ok": True}

    assert classify_provider_errors([{"type": "provider.content-filter", "status": 400}]) is ContentBlockedOpenCodeError
    assert classify_provider_errors([{"type": "provider.rate-limit", "status": 429, "message": "ttft_admission_refused"}]) is TransientOpenCodeError
    assert classify_provider_errors([{"status": 401, "message": "unauthorized"}]) is FatalOpenCodeError


def test_gate1_l0_to_l1_accept_reject_and_checkpoint(tmp_path: Path) -> None:
    items = [
        L0RawItem(
            id="atlas_good",
            source="atlas-methods",
            source_ref="repo/good",
            title="架构评审方法",
            raw_text="先看边界再看耦合，逐项检查失败模式。",
            content_hash=content_sha256("good"),
        ),
        L0RawItem(
            id="atlas_noise",
            source="atlas-methods",
            source_ref="repo/noise",
            title="某公司内部脚本参数说明",
            raw_text="运行 ./internal.sh --foo=bar",
            content_hash=content_sha256("noise"),
        ),
    ]
    llm = FakeLLM(
        [
            {
                "accept": True,
                "quality_score": 0.9,
                "reject_reason": None,
                "title": "系统边界与耦合诊断",
                "domain_hints": ["architecture"],
                "problem_solved": "架构混乱无从下手",
                "when_to_use": "设计评审前",
                "procedural_steps": ["画出核心模块边界", "标出跨边界状态共享"],
                "anti_patterns": ["只看代码命名不看耦合"],
                "refined_content": "按模块边界与依赖方向逐层检查架构脆弱点，输出前 3 个高风险耦合点。",
            },
            {
                "accept": False,
                "quality_score": 0.25,
                "reject_reason": "高度绑定私有内部脚本，无跨场景认知价值",
            },
        ]
    )
    out_dir = tmp_path / "l1"
    q_dir = tmp_path / "quarantine"

    s1 = run_l0_to_l1(items, llm=llm, out_dir=out_dir, quarantine_dir=q_dir, workers=1)
    assert s1.accepted == 1
    assert s1.rejected == 1
    assert s1.skipped_cached == 0
    assert (out_dir / "atlas_good.json").exists()
    assert (q_dir / "l0_rejected" / "atlas_noise.json").exists()

    # Re-run with unchanged hashes -> both must be skipped from cache without calling LLM
    s2 = run_l0_to_l1(items, llm=llm, out_dir=out_dir, quarantine_dir=q_dir, workers=1)
    assert s2.skipped_cached == 2
    assert llm.calls == 2


def test_gate2_gate3_and_gate4_full_flow_with_rollback(tmp_path: Path) -> None:
    l2_dir = tmp_path / "l2_lenses"
    q_dir = tmp_path / "quarantine"
    dist_dir = tmp_path / "dist"
    overlay_path = tmp_path / "overrides.yaml"

    mats = [
        L1Material(
            id="card_arch_1",
            source="prompt-taxonomy",
            source_ref="pt#1",
            title="架构 MECE 拆解",
            content="请按 MECE 拆解 {{audience}} 关心的核心模块：\n{{input}}",
            content_hash=content_sha256("arch1"),
        ),
        L1Material(
            id="card_arch_dup",
            source="atlas-methods",
            source_ref="atlas#2",
            title="架构拆解重复卡",
            content="请按模块拆解架构问题",
            content_hash=content_sha256("arch_dup"),
        ),
        L1Material(
            id="card_bad_enum",
            source="prompt-taxonomy",
            source_ref="pt#3",
            title="坏枚举测试卡",
            content="坏卡内容",
            content_hash=content_sha256("bad"),
        ),
    ]

    llm = FakeLLM(
        [
            # Card 1: uses alias "structure" -> normalized to "deconstruct", wrap mode has {{input}} + {{audience}}
            {
                "confidence": 0.92,
                "title": "架构 MECE 拆解",
                "tagline": "把一团麻的架构拆出清晰边界",
                "routing": {
                    "primitive": "structure",
                    "goals": ["improve"],
                    "clarity_fit": ["vague", "concrete"],
                    "domains": ["architecture"],
                    "app_affinity": ["Cursor"],
                    "pain_point": "架构思路混乱耦合严重",
                    "trigger_utterances": ["帮我分析这个架构", "模块太乱不知道怎么拆"],
                },
                "execution": {
                    "action_type": "inject_prompt",
                    "inject_mode": "wrap",
                    "content": "请按 MECE 拆解 {{audience}} 关心的核心模块：\n{{input}}",
                    "variables": [],
                },
            },
            # Card 2: valid schema, but semantically identical vector to Card 1 -> Gate 3 intercepts!
            {
                "confidence": 0.88,
                "title": "架构模块拆分",
                "tagline": "拆解复杂架构边界",
                "routing": {
                    "primitive": "deconstruct",
                    "goals": ["optimize"],
                    "clarity_fit": ["concrete"],
                    "domains": ["architecture"],
                    "app_affinity": ["Cursor"],
                    "pain_point": "架构思路混乱耦合严重",
                    "trigger_utterances": ["帮我分析这个架构", "系统怎么拆模块"],
                },
                "execution": {
                    "action_type": "inject_prompt",
                    "inject_mode": "append",
                    "content": "请按模块拆解架构问题。",
                },
            },
            # Card 3: unknown primitive -> Gate 2 quarantines!
            {
                "confidence": 0.90,
                "title": "未知操作卡",
                "tagline": "测试未知枚举",
                "routing": {
                    "primitive": "teleport_magic",
                    "goals": ["create"],
                    "clarity_fit": ["vague"],
                    "pain_point": "测试痛点描述",
                    "trigger_utterances": ["口语1", "口语2"],
                },
                "execution": {
                    "action_type": "inject_prompt",
                    "inject_mode": "append",
                    "content": "测试正文内容。",
                },
            },
        ]
    )

    embedder = FakeEmbedder({"架构": [1.0, 0.0, 0.0, 0.0]})
    summary = compile_l1_to_l2(
        mats,
        llm=llm,
        l2_dir=l2_dir,
        quarantine_dir=q_dir,
        embedder=embedder,
    )
    assert summary.compiled == 1
    assert summary.quarantined == 2

    active_lenses = load_existing_l2_lenses(l2_dir)
    assert "card_arch_1" in active_lenses
    assert active_lenses["card_arch_1"].routing.primitive == "deconstruct"
    assert active_lenses["card_arch_1"].execution.variables == ["audience"]

    # Check quarantine reasons
    dup_q = yaml.safe_load((q_dir / "card_arch_dup.yaml").read_text(encoding="utf-8"))
    assert dup_q["duplicate_of"] == "card_arch_1"
    bad_q = yaml.safe_load((q_dir / "card_bad_enum.yaml").read_text(encoding="utf-8"))
    assert any("unknown_primitive" in r for r in bad_q["reasons"])

    # Gate 4: Build release v1
    overlay_path.write_text("overrides: {}\n", encoding="utf-8")
    rel1, m1 = build_release(
        l2_dir=l2_dir,
        overlay_path=overlay_path,
        quarantine_dir=q_dir,
        dist_dir=dist_dir,
        embedder=embedder,
    )
    assert m1.total_active_lenses == 1
    assert m1.embedding_dim == 4
    assert (dist_dir / "current" / "lenses.json").exists()
    assert (dist_dir / "current" / "embeddings.npz").exists()

    # Apply local Overlay override & build release v2
    time.sleep(1.05)
    overlay_path.write_text(
        yaml.safe_dump({"overrides": {"card_arch_1": {"title": "架构拆解（本地覆写）", "base_weight": 1.8}}}),
        encoding="utf-8",
    )
    rel2, m2 = build_release(
        l2_dir=l2_dir,
        overlay_path=overlay_path,
        quarantine_dir=q_dir,
        dist_dir=dist_dir,
        embedder=embedder,
    )
    assert rel2.name != rel1.name
    v2_lenses = json.loads((dist_dir / "current" / "lenses.json").read_text(encoding="utf-8"))
    assert v2_lenses[0]["title"] == "架构拆解（本地覆写）"
    assert v2_lenses[0]["meta"]["base_weight"] == 1.8
    # Source YAML remains untouched
    assert load_existing_l2_lenses(l2_dir)["card_arch_1"].title == "架构 MECE 拆解"

    # Rollback to v1
    rolled = rollback_release(dist_dir)
    assert rolled == rel1.name
    v1_lenses = json.loads((dist_dir / "current" / "lenses.json").read_text(encoding="utf-8"))
    assert v1_lenses[0]["title"] == "架构 MECE 拆解"


def test_glint_core_adapter_reads_existing_cards() -> None:
    cards_yaml = LIB_ROOT.parent / "brain-server" / "data" / "cards.yaml"
    mats = load_glint_core_l1(cards_yaml)
    assert len(mats) == 8
    assert all(m.source == "glint-core" for m in mats)


def test_admin_api_smoke() -> None:
    from fastapi.testclient import TestClient

    from admin.server import create_admin_app

    client = TestClient(create_admin_app())
    r_index = client.get("/")
    assert r_index.status_code == 200
    assert "Glint 微光" in r_index.text

    r_ov = client.get("/api/overview")
    assert r_ov.status_code == 200
    body = r_ov.json()
    assert "ontology" in body and "l2_by_primitive" in body
    assert "taxonomy" in body and "shelf_policy" in body
    assert "by_tier" in body and "by_category_l3" in body and "by_category_l4" in body

    r_lenses = client.get("/api/lenses")
    assert r_lenses.status_code == 200
    assert "items" in r_lenses.json()

    r_q = client.get("/api/quarantine")
    assert r_q.status_code == 200
    assert "l2_quarantined" in r_q.json() and "l0_rejected" in r_q.json()

    r_evo = client.get("/api/evolution/analytics")
    assert r_evo.status_code == 200
    assert "total_events" in r_evo.json() and "ctr" in r_evo.json()

    r_prop = client.get("/api/evolution/proposals")
    assert r_prop.status_code == 200
    assert "demote_to_l3" in r_prop.json() and "promote_to_l4" in r_prop.json()

    r_cfg = client.get("/api/config/routing_policy")
    assert r_cfg.status_code == 200
    assert "yaml_text" in r_cfg.json()


def test_l2_to_l3_deterministic_cleaning_and_priority_dedup(tmp_path: Path) -> None:
    from pipeline.l2_to_l3 import run_l2_to_l3
    from pipeline.schema import L2Lens, LensExecution, LensMeta, LensRouting

    l2_dir = tmp_path / "l2"
    l3_dir = tmp_path / "l3"
    q_dir = tmp_path / "quarantine"
    ov_path = tmp_path / "overrides.yaml"
    ov_path.write_text("overrides: {}\n", encoding="utf-8")

    # Dirty L2 draft: append mode with stray {{input}}, Chinese 【变量】, unnormalized domains/apps, boilerplate pain_point
    dirty = L2Lens(
        id="core_dirty_arch",
        title="【架构拆解】。",
        tagline="这是一个用于拆解架构的模板，帮用户理清边界",
        routing=LensRouting(
            primitive="deconstruct",
            goals=["optimize"],
            clarity_fit=["concrete"],
            domains=["系统架构", "coding"],
            app_affinity=["vscode", "飞书"],
            pain_point="该模板用于解决架构太乱无从下手的问题",
            trigger_utterances=["“帮我拆一下架构”", "这架构太乱了", "这架构太乱了"],
        ),
        execution=LensExecution(
            action_type="inject_prompt",
            inject_mode="append",
            content="请按【目标读者】视角拆解以下架构。{{input}}",
            variables=[],
        ),
        meta=LensMeta(
            source="glint-core",
            source_ref="core#dirty",
            source_hash=content_sha256("dirty"),
        ),
    )
    # Lower-priority duplicate from atlas-methods (should be dropped by priority dedup!)
    dup_low = L2Lens(
        id="atlas_dup_arch",
        title="架构模块拆分",
        tagline="拆分架构模块",
        routing=LensRouting(
            primitive="deconstruct",
            goals=["optimize"],
            clarity_fit=["concrete"],
            domains=["architecture"],
            app_affinity=["Cursor"],
            pain_point="架构太乱无从下手",
            trigger_utterances=["帮我拆架构", "理一下架构"],
        ),
        execution=LensExecution(
            action_type="inject_prompt",
            inject_mode="append",
            content="请拆解模块边界。",
            variables=[],
        ),
        meta=LensMeta(
            source="atlas-methods",
            source_ref="atlas#dup",
            source_hash=content_sha256("dup"),
        ),
    )
    for item in (dirty, dup_low):
        p = l2_dir / item.routing.primitive / f"{item.id}.yaml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(item.model_dump(), allow_unicode=True), encoding="utf-8")

    embedder = FakeEmbedder({"架构": [1.0, 0.0, 0.0, 0.0]})
    kept, vecs, report = run_l2_to_l3(
        l2_dir=l2_dir,
        l3_dir=l3_dir,
        overlay_path=ov_path,
        quarantine_dir=q_dir,
        embedder=embedder,
    )
    assert len(kept) == 1
    assert kept[0].id == "core_dirty_arch"  # glint-core beat atlas-methods
    assert kept[0].title == "架构拆解"
    assert kept[0].routing.domains == ["architecture", "engineering"]
    assert kept[0].routing.app_affinity == ["VS Code", "Feishu"]
    assert "{{input}}" not in kept[0].execution.content
    assert "目标读者" in kept[0].execution.variables
    assert not kept[0].routing.pain_point.startswith("该模板用于")
    assert kept[0].routing.trigger_utterances == ["帮我拆一下架构", "这架构太乱了"]
    assert report.total_quarantined_dedup == 1
    assert vecs is not None and vecs.shape == (1, 4)
    # Tier & taxonomy should be materialized automatically
    assert kept[0].meta.tier == "A"  # glint-core is in tier_a_sources (no longer hardcoded Tier S)
    assert kept[0].meta.category == "engineering"
    assert kept[0].meta.subcategory == "architecture"


def test_declarative_shelf_rules_and_tier_s_protection(tmp_path: Path) -> None:
    from pipeline.evolution import compute_shelf_proposals
    from pipeline.l2_to_l3 import run_l2_to_l3
    from pipeline.schema import L2Lens, LensExecution, LensMeta, LensRouting

    l2_dir = tmp_path / "l2"
    l3_dir = tmp_path / "l3"
    q_dir = tmp_path / "quarantine"
    ov_path = tmp_path / "overrides.yaml"
    ov_path.write_text("overrides: {}\n", encoding="utf-8")

    policy_path = tmp_path / "shelf_policy.yaml"
    policy_path.write_text(
        yaml.safe_dump(
            {
                "version": "2.3",
                "always_on_tier_s": True,
                "tier_a_min_confidence": 0.85,
                "off_shelf_sources": ["atlas-methods"],
                "category_modes": {
                    "engineering": "tier_a",
                    "product_business": "off",
                },
                "active_collections": ["sprint_pack"],
                "collections": {
                    "sprint_pack": {
                        "name": "冲刺包",
                        "lens_ids": ["mkt_in_collection"],
                    }
                },
                "evergreen_ids": ["core_tier_s"],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    def _make(lid: str, source: str, domains: list[str], conf: float) -> L2Lens:
        return L2Lens(
            id=lid,
            title=f"标题-{lid}",
            tagline=f"副标题-{lid}",
            routing=LensRouting(
                primitive="deconstruct",
                goals=["optimize"],
                clarity_fit=["concrete"],
                domains=domains,
                app_affinity=["Cursor"],
                pain_point=f"痛点-{lid}",
                trigger_utterances=[f"触发1-{lid}", f"触发2-{lid}"],
            ),
            execution=LensExecution(
                action_type="inject_prompt",
                inject_mode="append",
                content=f"正文内容-{lid}",
                variables=[],
            ),
            meta=LensMeta(
                source=source,
                source_ref=f"{source}#{lid}",
                source_hash=content_sha256(lid),
                confidence=conf,
            ),
        )

    lenses = [
        _make("core_tier_s", "glint-core", ["architecture"], 0.90),
        _make("eng_tier_a", "prompt-taxonomy", ["architecture"], 0.92),
        _make("eng_tier_b", "prompt-taxonomy", ["architecture"], 0.75),
        _make("mkt_off", "prompt-taxonomy", ["marketing"], 0.95),
        _make("mkt_in_collection", "prompt-taxonomy", ["marketing"], 0.70),
        _make("atlas_off", "atlas-methods", ["architecture"], 0.95),
    ]
    for item in lenses:
        p = l2_dir / item.routing.primitive / f"{item.id}.yaml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(item.model_dump(), allow_unicode=True), encoding="utf-8")

    # Distinct vectors so dedup doesn't merge them
    embedder = FakeEmbedder(
        {
            "core_tier_s": [1.0, 0.0, 0.0, 0.0],
            "eng_tier_a": [0.0, 1.0, 0.0, 0.0],
            "eng_tier_b": [0.0, 0.0, 1.0, 0.0],
            "mkt_off": [0.0, 0.0, 0.0, 1.0],
            "mkt_in_collection": [0.7, 0.7, 0.0, 0.0],
            "atlas_off": [0.0, 0.7, 0.7, 0.0],
        }
    )

    kept, _, _ = run_l2_to_l3(
        l2_dir=l2_dir,
        l3_dir=l3_dir,
        overlay_path=ov_path,
        quarantine_dir=q_dir,
        shelf_policy_path=policy_path,
        embedder=embedder,
    )
    by_id = {x.id: x for x in kept}
    assert by_id["core_tier_s"].meta.tier == "S"
    assert by_id["core_tier_s"].meta.shelf == "evergreen"
    assert by_id["eng_tier_a"].meta.tier == "A"
    assert by_id["eng_tier_a"].meta.shelf == "on_shelf"
    assert by_id["eng_tier_b"].meta.tier == "B"
    assert by_id["eng_tier_b"].meta.shelf == "off_shelf"
    assert by_id["mkt_off"].meta.category == "product_business"
    assert by_id["mkt_off"].meta.subcategory == "growth_marketing"
    assert by_id["mkt_off"].meta.shelf == "off_shelf"  # category mode is off
    assert by_id["mkt_in_collection"].meta.shelf == "on_shelf"  # pulled in by active_collections!
    assert "sprint_pack" in by_id["mkt_in_collection"].meta.collections
    assert by_id["atlas_off"].meta.shelf == "off_shelf"  # off_shelf_sources

    # Evolution scheduler must NEVER propose demoting Tier S / evergreen cards
    import sqlite3
    from datetime import datetime, timezone

    db_path = tmp_path / "usage_log.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            app_name TEXT,
            input_text TEXT,
            recommended_ids TEXT,
            selected_id TEXT,
            custom_intent TEXT,
            latency_ms REAL,
            predicted_primitive TEXT,
            predicted_goals TEXT,
            predicted_clarity TEXT
        )
        """
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    for _ in range(10):
        conn.execute(
            "INSERT INTO usage_logs (timestamp, app_name, input_text, recommended_ids, selected_id, latency_ms) VALUES (?, ?, ?, ?, ?, ?)",
            (now_iso, "Cursor", "测试输入", json.dumps(["core_tier_s", "eng_tier_a"]), None, 250.0),
        )
    conn.commit()
    conn.close()

    proposals = compute_shelf_proposals(db_path=db_path, l3_dir=l3_dir, min_impressions_demote=5)
    demote_ids = [p["id"] for p in proposals["demote_to_l3"]]
    assert "core_tier_s" not in demote_ids
    assert "eng_tier_a" in demote_ids
    assert proposals["evergreen_protected"] == 1
