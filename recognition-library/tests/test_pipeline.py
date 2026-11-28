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
    assert len(mats) == 9
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


def test_meta_vault_merges_without_polluting_public_l3(tmp_path: Path) -> None:
    from pipeline.l2_to_l3 import run_l2_to_l3

    pub_l2 = tmp_path / "pub" / "l2_lenses" / "deconstruct"
    pub_l3 = tmp_path / "pub" / "l3_production"
    overlay_path = tmp_path / "pub" / "overrides.yaml"
    quarantine_dir = tmp_path / "pub" / "quarantine"
    vault_dir = tmp_path / "meta-vault"
    vault_l2 = vault_dir / "l2_lenses" / "translate"

    pub_l2.mkdir(parents=True, exist_ok=True)
    vault_l2.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text("overrides: {}\n", encoding="utf-8")

    (pub_l2 / "pub_one.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "pub_one",
                "title": "公开卡片",
                "tagline": "公开一阶卡片测试",
                "routing": {
                    "primitive": "deconstruct",
                    "goals": ["optimize"],
                    "clarity_fit": ["concrete"],
                    "domains": ["architecture"],
                    "app_affinity": ["Cursor"],
                    "pain_point": "架构混乱不知道从哪拆",
                    "trigger_utterances": ["帮我拆解架构", "模块太乱了理一下"],
                    "anti_triggers": [],
                },
                "execution": {
                    "action_type": "inject_prompt",
                    "inject_mode": "append",
                    "content": "请帮我拆解这个架构。",
                    "variables": [],
                },
                "meta": {
                    "source": "glint-core",
                    "source_ref": "test#pub_one",
                    "source_hash": "abc123",
                    "confidence": 0.95,
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    (vault_dir / "shelf_policy.yaml").write_text(
        "evergreen_ids:\n  - secret_meta_one\n",
        encoding="utf-8",
    )
    (vault_l2 / "secret_meta_one.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "secret_meta_one",
                "title": "私有二阶算子",
                "tagline": "私有二阶元认知卡片测试",
                "routing": {
                    "primitive": "translate",
                    "goals": ["create"],
                    "clarity_fit": ["vague"],
                    "domains": ["prompt_engineering"],
                    "app_affinity": ["Obsidian"],
                    "pain_point": "把直觉转成可复用规则",
                    "trigger_utterances": ["把直觉转成卡片", "锻造可复用提示词"],
                    "anti_triggers": [],
                },
                "execution": {
                    "action_type": "inject_prompt",
                    "inject_mode": "append",
                    "content": "请将我的直觉锻造为可复用卡片。",
                    "variables": [],
                },
                "meta": {
                    "source": "glint-core",
                    "source_ref": "vault#secret",
                    "source_hash": "def456",
                    "confidence": 0.95,
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    kept, _, report = run_l2_to_l3(
        l2_dir=tmp_path / "pub" / "l2_lenses",
        l3_dir=pub_l3,
        overlay_path=overlay_path,
        quarantine_dir=quarantine_dir,
        meta_vault_dir=vault_dir,
    )

    kept_ids = {l.id for l in kept}
    assert kept_ids == {"pub_one", "secret_meta_one"}
    assert report.total_l2_input == 1
    assert report.total_l3_active == 1
    assert (pub_l3 / "deconstruct" / "pub_one.yaml").exists()
    assert not (pub_l3 / "translate" / "secret_meta_one.yaml").exists()
    vault_l3_item = vault_dir / "l3_production" / "translate" / "secret_meta_one.yaml"
    assert vault_l3_item.exists()
    vault_loaded = yaml.safe_load(vault_l3_item.read_text(encoding="utf-8"))
    assert vault_loaded["meta"]["shelf"] == "evergreen"
    assert vault_loaded["meta"]["tier"] == "S"


def test_vault_duplicate_validation_stale_cleanup_and_admin_isolation(tmp_path: Path) -> None:
    import pytest
    from fastapi.testclient import TestClient

    import admin.server as admin_srv
    from pipeline.builder import validate_l2_repository
    from pipeline.l2_to_l3 import run_l2_to_l3
    from pipeline.schema import L2Lens, LensExecution, LensMeta, LensRouting

    def _mk(lid: str, source: str = "glint-core", triggers: list[str] | None = None, title: str | None = None) -> L2Lens:
        return L2Lens(
            id=lid,
            title=title or f"卡片-{lid}",
            tagline="把模糊输入拆解为可验证前提",
            routing=LensRouting(
                primitive="deconstruct",
                goals=["digest"],
                clarity_fit=["concrete"],
                domains=["architecture"],
                pain_point="面对复杂结构不知道如何拆解",
                trigger_utterances=triggers if triggers is not None else ["帮我拆解", "理一下结构"],
            ),
            execution=LensExecution(action_type="inject_prompt", inject_mode="append", content="请按前提逐项拆解。"),
            meta=LensMeta(source=source, source_ref="test#item", source_hash=content_sha256(lid), confidence=0.9),
        )

    l2_dir = tmp_path / "l2_lenses"
    l3_dir = tmp_path / "l3_production"
    q_dir = tmp_path / "quarantine"
    ov_path = tmp_path / "overlays" / "overrides.yaml"
    dist_dir = tmp_path / "dist"
    vault_dir = tmp_path / "meta-vault"
    (l2_dir / "deconstruct").mkdir(parents=True)
    (vault_dir / "l2_lenses" / "deconstruct").mkdir(parents=True)
    ov_path.parent.mkdir(parents=True)
    ov_path.write_text("overrides: {}\n", encoding="utf-8")

    # 1. Duplicate ID + single-trigger failure detected explicitly by validate_l2_repository
    (l2_dir / "deconstruct" / "dup_id.yaml").write_text(
        yaml.safe_dump(_mk("dup_id", title="公开卡片").model_dump(), allow_unicode=True), encoding="utf-8"
    )
    (vault_dir / "l2_lenses" / "deconstruct" / "dup_id.yaml").write_text(
        yaml.safe_dump(_mk("dup_id", title="私有卡片").model_dump(), allow_unicode=True), encoding="utf-8"
    )
    (vault_dir / "l2_lenses" / "deconstruct" / "vault_1trig.yaml").write_text(
        yaml.safe_dump(_mk("vault_1trig", triggers=["只有一条"]).model_dump(), allow_unicode=True), encoding="utf-8"
    )
    _, errs = validate_l2_repository(l2_dir, ov_path, meta_vault_dir=vault_dir)
    assert any("duplicate lens ID" in e for e in errs)
    assert any("vault_1trig: insufficient trigger_utterances" in e for e in errs)

    # 2. run_l2_to_l3 raises on duplicate ID rather than silently overwriting
    with pytest.raises(ValueError, match="Duplicate lens IDs"):
        run_l2_to_l3(l2_dir=l2_dir, l3_dir=l3_dir, overlay_path=ov_path, quarantine_dir=q_dir, meta_vault_dir=vault_dir)

    # Remove duplicate conflict; vault_1trig fails rule gate -> written to vault quarantine & stale vault l3 cleared!
    (vault_dir / "l2_lenses" / "deconstruct" / "dup_id.yaml").unlink()
    (vault_dir / "l3_production" / "deconstruct").mkdir(parents=True)
    (vault_dir / "l3_production" / "deconstruct" / "stale_vault.yaml").write_text("id: stale\n", encoding="utf-8")
    _, _, rep = run_l2_to_l3(
        l2_dir=l2_dir, l3_dir=l3_dir, overlay_path=ov_path, quarantine_dir=q_dir, meta_vault_dir=vault_dir
    )
    assert rep.total_quarantined_rules == 0  # never leaks into public cleaning report
    assert (vault_dir / "quarantine" / "l3_rules" / "vault_1trig.yaml").exists()
    assert not (vault_dir / "l3_production" / "deconstruct" / "stale_vault.yaml").exists()

    # 3. Admin console recognizes vault lenses, keeps vault overlays private, and cleans L3 on personal delete
    (vault_dir / "l2_lenses" / "deconstruct" / "vault_1trig.yaml").unlink()
    (vault_dir / "l2_lenses" / "deconstruct" / "vault_only.yaml").write_text(
        yaml.safe_dump(_mk("vault_only", title="私有透镜").model_dump(), allow_unicode=True), encoding="utf-8"
    )
    (l2_dir / "deconstruct" / "usr_personal_1.yaml").write_text(
        yaml.safe_dump(_mk("usr_personal_1", source="user-forge", title="自建卡").model_dump(), allow_unicode=True),
        encoding="utf-8",
    )
    build_release(
        l2_dir=l2_dir, l3_dir=l3_dir, overlay_path=ov_path, quarantine_dir=q_dir,
        dist_dir=dist_dir, embedder=None, meta_vault_dir=vault_dir,
    )

    orig = (admin_srv.L2_DIR, admin_srv.L3_DIR, admin_srv.OVERLAY_PATH, admin_srv.QUARANTINE_DIR, admin_srv.DIST_DIR, admin_srv.META_VAULT_DIR)
    try:
        admin_srv.L2_DIR = l2_dir
        admin_srv.L3_DIR = l3_dir
        admin_srv.OVERLAY_PATH = ov_path
        admin_srv.QUARANTINE_DIR = q_dir
        admin_srv.DIST_DIR = dist_dir
        admin_srv.META_VAULT_DIR = vault_dir
        client = TestClient(admin_srv.create_admin_app())

        ov_resp = client.get("/api/overview").json()
        assert ov_resp["l4_total"] == ov_resp["live_l4_total"] == 3
        assert ov_resp["needs_rebuild"] is False

        lenses_resp = client.get("/api/lenses").json()
        by_id = {it["id"]: it for it in lenses_resp["items"]}
        assert "vault_only" in by_id and by_id["vault_only"]["is_vault"] is True

        # Vault overlay saved strictly into meta-vault/overrides.yaml, keeping public overrides.yaml clean
        put_ov = client.put("/api/overlays/vault_only", json={"tier": "S"})
        assert put_ov.status_code == 200
        assert (vault_dir / "overrides.yaml").exists()
        assert "vault_only" in (vault_dir / "overrides.yaml").read_text(encoding="utf-8")
        assert "vault_only" not in ov_path.read_text(encoding="utf-8")

        # Personal card deletion immediately removes from both L2 and L3
        del_resp = client.delete("/api/lenses/usr_personal_1")
        assert del_resp.status_code == 200 and del_resp.json()["removed_files"] == 2
        after_ids = {it["id"] for it in client.get("/api/lenses").json()["items"]}
        assert "usr_personal_1" not in after_ids
    finally:
        (admin_srv.L2_DIR, admin_srv.L3_DIR, admin_srv.OVERLAY_PATH, admin_srv.QUARANTINE_DIR, admin_srv.DIST_DIR, admin_srv.META_VAULT_DIR) = orig


def test_meta_vault_collections_isolation_and_write_routing(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    import admin.server as admin_srv
    from pipeline.l2_to_l3 import _merge_vault_shelf_policy, run_l2_to_l3
    from pipeline.schema import L2Lens, LensExecution, LensMeta, LensRouting

    def _mk(lid: str, title: str) -> L2Lens:
        return L2Lens(
            id=lid,
            title=title,
            tagline="测试副标题",
            routing=LensRouting(
                primitive="deconstruct",
                goals=["digest"],
                clarity_fit=["concrete"],
                domains=["architecture"],
                pain_point="测试痛点描述",
                trigger_utterances=["测试触发语一", "测试触发语二"],
            ),
            execution=LensExecution(action_type="inject_prompt", inject_mode="append", content="请按结构拆解。"),
            meta=LensMeta(source="glint-core", source_ref="test#col", source_hash=content_sha256(lid), confidence=0.9),
        )

    l2_dir = tmp_path / "l2_lenses"
    l3_dir = tmp_path / "l3_production"
    q_dir = tmp_path / "quarantine"
    ov_path = tmp_path / "overrides.yaml"
    dist_dir = tmp_path / "dist"
    pub_sp_path = tmp_path / "shelf_policy.yaml"
    vault_dir = tmp_path / "meta-vault"

    (l2_dir / "deconstruct").mkdir(parents=True)
    (vault_dir / "l2_lenses" / "deconstruct").mkdir(parents=True)
    ov_path.write_text("overrides: {}\n", encoding="utf-8")

    (l2_dir / "deconstruct" / "pub_card.yaml").write_text(
        yaml.safe_dump(_mk("pub_card", "公开卡片").model_dump(), allow_unicode=True), encoding="utf-8"
    )
    (vault_dir / "l2_lenses" / "deconstruct" / "vault_card.yaml").write_text(
        yaml.safe_dump(_mk("vault_card", "私有二阶卡").model_dump(), allow_unicode=True), encoding="utf-8"
    )

    pub_sp_initial = yaml.safe_dump(
        {
            "version": "2.3",
            "always_on_tier_s": True,
            "category_modes": {"engineering": "all"},
            "active_collections": [],
            "collections": {"pub_pack": {"name": "公开专题包", "lens_ids": ["pub_card"]}},
            "evergreen_ids": [],
        },
        allow_unicode=True,
        sort_keys=False,
    )
    pub_sp_path.write_text(pub_sp_initial, encoding="utf-8")

    (vault_dir / "shelf_policy.yaml").write_text(
        yaml.safe_dump(
            {
                "version": "1.0",
                "evergreen_ids": ["vault_card"],
                "active_collections": ["vault_pack"],
                "collections": {
                    "vault_pack": {
                        "name": "私有专题包",
                        "lens_ids": ["vault_card", "pub_card"],
                    }
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # 1. Merge sets is_vault=True on vault collections; public L3 never persists vault collection IDs
    merged_sp = _merge_vault_shelf_policy(yaml.safe_load(pub_sp_initial), vault_dir)
    assert merged_sp["collections"]["vault_pack"]["is_vault"] is True
    assert merged_sp["collections"]["pub_pack"]["is_vault"] is False
    assert "vault_pack" in merged_sp["active_collections"]

    kept, _, _ = run_l2_to_l3(
        l2_dir=l2_dir,
        l3_dir=l3_dir,
        overlay_path=ov_path,
        quarantine_dir=q_dir,
        shelf_policy_path=pub_sp_path,
        meta_vault_dir=vault_dir,
    )
    assert any(l.id == "vault_card" and "vault_pack" in l.meta.collections for l in kept)
    pub_l3_data = yaml.safe_load((l3_dir / "deconstruct" / "pub_card.yaml").read_text(encoding="utf-8"))
    assert "vault_pack" not in pub_l3_data["meta"]["collections"]

    # 2. Admin API exposes vault collection, routes toggles/creates to vault, and blocks leaking vault cards into public collections
    orig = (
        admin_srv.L2_DIR,
        admin_srv.L3_DIR,
        admin_srv.OVERLAY_PATH,
        admin_srv.QUARANTINE_DIR,
        admin_srv.DIST_DIR,
        admin_srv.META_VAULT_DIR,
        admin_srv.DEFAULT_SHELF_POLICY_PATH,
    )
    try:
        admin_srv.L2_DIR = l2_dir
        admin_srv.L3_DIR = l3_dir
        admin_srv.OVERLAY_PATH = ov_path
        admin_srv.QUARANTINE_DIR = q_dir
        admin_srv.DIST_DIR = dist_dir
        admin_srv.META_VAULT_DIR = vault_dir
        admin_srv.DEFAULT_SHELF_POLICY_PATH = pub_sp_path
        client = TestClient(admin_srv.create_admin_app())

        ov = client.get("/api/overview").json()
        assert ov["shelf_policy"]["collections"]["vault_pack"]["is_vault"] is True
        assert "vault_pack" in ov["shelf_policy"]["active_collections"]

        # Filtering by vault collection works
        filtered = client.get("/api/lenses?collection=vault_pack").json()["items"]
        assert "vault_card" in {it["id"] for it in filtered}

        # Toggling vault collection updates ONLY meta-vault/shelf_policy.yaml
        r_tog = client.put("/api/shelf/collections/vault_pack/toggle", json={"active": False})
        assert r_tog.status_code == 200
        assert "vault_pack" not in (yaml.safe_load((vault_dir / "shelf_policy.yaml").read_text(encoding="utf-8")).get("active_collections") or [])
        assert pub_sp_path.read_text(encoding="utf-8") == pub_sp_initial

        # Attempting to batch-add a vault card into a public collection is blocked with 400
        r_leak = client.post("/api/overlays/batch", json={"lens_ids": ["vault_card"], "add_collection": "pub_pack"})
        assert r_leak.status_code == 400
        assert "vault_card" not in pub_sp_path.read_text(encoding="utf-8")

        # Creating a collection containing a vault card auto-routes into meta-vault/shelf_policy.yaml
        r_create = client.post("/api/shelf/collections", json={"name": "新私库包", "lens_ids": ["vault_card"], "active": True})
        assert r_create.status_code == 200 and r_create.json()["is_vault"] is True
        assert "vault_card" not in pub_sp_path.read_text(encoding="utf-8")
        assert r_create.json()["id"] in yaml.safe_load((vault_dir / "shelf_policy.yaml").read_text(encoding="utf-8"))["collections"]
    finally:
        (
            admin_srv.L2_DIR,
            admin_srv.L3_DIR,
            admin_srv.OVERLAY_PATH,
            admin_srv.QUARANTINE_DIR,
            admin_srv.DIST_DIR,
            admin_srv.META_VAULT_DIR,
            admin_srv.DEFAULT_SHELF_POLICY_PATH,
        ) = orig


def test_card_import_parser_and_lineage_archiving(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    import admin.server as admin_srv
    from pipeline.forge import parse_card_import_text
    from pipeline.schema import L2Lens, LensExecution, LensMeta, LensRouting

    l2_dir = tmp_path / "l2_lenses"
    l3_dir = tmp_path / "l3_production"
    q_dir = tmp_path / "quarantine"
    ov_path = tmp_path / "overrides.yaml"
    dist_dir = tmp_path / "dist"
    (l2_dir / "deconstruct").mkdir(parents=True)
    ov_path.write_text("overrides: {}\n", encoding="utf-8")

    old_lens = L2Lens(
        id="old_card_v1",
        title="架构边界拆解",
        tagline="旧版一句话",
        routing=LensRouting(
            primitive="deconstruct",
            goals=["optimize"],
            clarity_fit=["concrete"],
            domains=["architecture"],
            pain_point="旧版痛点描述",
            trigger_utterances=["架构拆解", "怎么拆模块"],
        ),
        execution=LensExecution(action_type="inject_prompt", inject_mode="append", content="旧版指令正文。"),
        meta=LensMeta(source="manual-custom", source_ref="manual#old", source_hash=content_sha256("old"), version=1),
    )
    (l2_dir / "deconstruct" / "old_card_v1.yaml").write_text(
        yaml.safe_dump(old_lens.model_dump(), allow_unicode=True), encoding="utf-8"
    )

    llm_reply = """1) 反模式：空泛堆砌形容词
2) 核心机制：按耦合度排序

```yaml
title: 架构边界拆解
tagline: 3步拆出模块边界与最脆弱耦合点
primitive: 解构建模
category: engineering
tier: S
inject_mode: append
pain_point: 系统模块搅成一团，不知道从哪一层切分
trigger_utterances:
  - 架构拆解
  - 模块拆分
  - 帮我理清这个系统的模块边界
evolution_note: 外科锐化 v2，补上最脆弱耦合点红线
content: 请按边界与依赖方向把系统拆为 3-5 个模块，并指出最脆弱耦合点。
```
"""
    parsed = parse_card_import_text(llm_reply, existing_lenses={"old_card_v1": old_lens})
    assert parsed["ok"] is True
    assert parsed["card"]["primitive"] == "deconstruct"
    assert parsed["card"]["tier"] == "S"
    assert parsed["matched_predecessor"]["id"] == "old_card_v1"

    orig = (admin_srv.L2_DIR, admin_srv.L3_DIR, admin_srv.OVERLAY_PATH, admin_srv.QUARANTINE_DIR, admin_srv.DIST_DIR, admin_srv.META_VAULT_DIR)
    try:
        admin_srv.L2_DIR = l2_dir
        admin_srv.L3_DIR = l3_dir
        admin_srv.OVERLAY_PATH = ov_path
        admin_srv.QUARANTINE_DIR = q_dir
        admin_srv.DIST_DIR = dist_dir
        admin_srv.META_VAULT_DIR = None
        client = TestClient(admin_srv.create_admin_app())

        # Create v2 card replacing old_card_v1 (archives old via overlay, records bidirectional lineage)
        r_create = client.post(
            "/api/lenses/manual",
            json={
                **parsed["card"],
                "supersedes_id": "old_card_v1",
            },
        )
        assert r_create.status_code == 200
        body = r_create.json()
        new_id = body["lens"]["id"]
        assert body["archived_predecessor"] == "old_card_v1"
        assert body["lens"]["meta"]["version"] == 2
        assert body["lens"]["meta"]["supersedes"] == "old_card_v1"

        # Old card appears in archived tab with superseded_by pointing to new_id, and active tab excludes it
        archived = client.get("/api/lenses?shelf=archived").json()["items"]
        assert len(archived) == 1
        assert archived[0]["id"] == "old_card_v1"
        assert archived[0]["meta"]["status"] == "deprecated"
        assert archived[0]["meta"]["superseded_by"] == new_id

        active = client.get("/api/lenses?shelf=l4_all").json()["items"]
        active_ids = {it["id"] for it in active}
        assert new_id in active_ids
        assert "old_card_v1" not in active_ids

        # Regression 1: Re-archiving an already-deprecated predecessor returns 409 without creating a card
        before_files = set(l2_dir.rglob("*.yaml"))
        r_dup_arch = client.post(
            "/api/lenses/manual",
            json={**parsed["card"], "supersedes_id": "old_card_v1"},
        )
        assert r_dup_arch.status_code == 409
        assert set(l2_dir.rglob("*.yaml")) == before_files

        # Regression 2: Wrap mode without {{input}} auto-appends {{input}} and passes validate_l2_repository
        from pipeline.builder import validate_l2_repository

        r_wrap = client.post(
            "/api/lenses/manual",
            json={
                "title": "包裹模式自动补位",
                "tagline": "测试 wrap 自动补齐 input",
                "primitive": "translate",
                "category": "custom",
                "tier": "A",
                "action_type": "inject_prompt",
                "inject_mode": "wrap",
                "pain_point": "需要包裹输入但忘记写占位符",
                "trigger_utterances": ["包裹测试", "自动补位"],
                "content": "请帮我润色下面这段话：",
            },
        )
        assert r_wrap.status_code == 200
        assert "{{input}}" in r_wrap.json()["lens"]["execution"]["content"]
        _, val_errs = validate_l2_repository(l2_dir, ov_path)
        assert not val_errs

        # Regression 3: Fenced block without newline before closing ``` still parses cleanly
        inline_close = "```yaml\ntitle: 紧贴闭合卡\npain_point: 闭合反引号没有换行\ntrigger_utterances: [紧贴闭合, 闭合测试]\ncontent: 请直接执行这段测试指令。```"
        p_inline = parse_card_import_text(inline_close)
        assert p_inline["ok"] is True and p_inline["card"]["title"] == "紧贴闭合卡"

        # Regression 4: Vault predecessor cannot be superseded by a public card, and invalid vault+public collection leaves zero files
        vault_dir = tmp_path / "meta-vault"
        (vault_dir / "l2_lenses" / "translate").mkdir(parents=True)
        v_old = L2Lens(
            id="vault_old_v1",
            title="私库旧卡",
            tagline="私库旧版",
            routing=LensRouting(
                primitive="translate",
                goals=["optimize"],
                clarity_fit=["concrete"],
                domains=["prompt_engineering"],
                pain_point="私库旧版痛点",
                trigger_utterances=["私库旧卡", "私库测试"],
            ),
            execution=LensExecution(action_type="inject_prompt", inject_mode="append", content="私库旧正文。"),
            meta=LensMeta(source="glint-core", source_ref="vault#old", source_hash=content_sha256("vold"), version=1),
        )
        (vault_dir / "l2_lenses" / "translate" / "vault_old_v1.yaml").write_text(
            yaml.safe_dump(v_old.model_dump(), allow_unicode=True), encoding="utf-8"
        )
        admin_srv.META_VAULT_DIR = vault_dir
        client_v = TestClient(admin_srv.create_admin_app())

        pub_before = set(l2_dir.rglob("*.yaml"))
        vault_before = set((vault_dir / "l2_lenses").rglob("*.yaml"))
        r_cross = client_v.post(
            "/api/lenses/manual",
            json={**parsed["card"], "supersedes_id": "vault_old_v1", "target_vault": False},
        )
        assert r_cross.status_code == 400
        assert set(l2_dir.rglob("*.yaml")) == pub_before

        r_bad_col = client_v.post(
            "/api/lenses/manual",
            json={**parsed["card"], "supersedes_id": "vault_old_v1", "target_vault": True, "add_collection": "non_vault_col"},
        )
        assert r_bad_col.status_code == 400
        assert set((vault_dir / "l2_lenses").rglob("*.yaml")) == vault_before
        assert not (vault_dir / "overrides.yaml").exists()
    finally:
        (admin_srv.L2_DIR, admin_srv.L3_DIR, admin_srv.OVERLAY_PATH, admin_srv.QUARANTINE_DIR, admin_srv.DIST_DIR, admin_srv.META_VAULT_DIR) = orig


def test_sort_newest_single_card_collections_and_rename(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    import admin.server as admin_srv
    from pipeline.schema import L2Lens, LensExecution, LensMeta, LensRouting

    l2_dir = tmp_path / "l2_lenses"
    l3_dir = tmp_path / "l3_production"
    q_dir = tmp_path / "quarantine"
    ov_path = tmp_path / "overrides.yaml"
    dist_dir = tmp_path / "dist"
    pub_sp_path = tmp_path / "shelf_policy.yaml"
    vault_dir = tmp_path / "meta-vault"
    (l2_dir / "deconstruct").mkdir(parents=True)
    (vault_dir / "l2_lenses" / "translate").mkdir(parents=True)
    ov_path.write_text("overrides: {}\n", encoding="utf-8")

    older = L2Lens(
        id="card_older",
        title="老卡片",
        tagline="较早创建的卡",
        routing=LensRouting(
            primitive="deconstruct",
            goals=["optimize"],
            clarity_fit=["concrete"],
            domains=["general"],
            pain_point="旧痛点描述",
            trigger_utterances=["老卡片", "旧测试"],
        ),
        execution=LensExecution(action_type="inject_prompt", inject_mode="append", content="老卡片正文。"),
        meta=LensMeta(
            source="manual-custom",
            source_ref="manual#old",
            source_hash=content_sha256("older"),
            created_at="2026-11-01T10:00:00+0800",
        ),
    )
    (l2_dir / "deconstruct" / "card_older.yaml").write_text(
        yaml.safe_dump(older.model_dump(), allow_unicode=True), encoding="utf-8"
    )
    v_card = L2Lens(
        id="vault_secret_1",
        title="私库卡",
        tagline="私库卡片",
        routing=LensRouting(
            primitive="translate",
            goals=["optimize"],
            clarity_fit=["concrete"],
            domains=["general"],
            pain_point="私库痛点描述",
            trigger_utterances=["私库卡", "私库测试"],
        ),
        execution=LensExecution(action_type="inject_prompt", inject_mode="append", content="私库正文。"),
        meta=LensMeta(source="glint-core", source_ref="vault#1", source_hash=content_sha256("v1")),
    )
    (vault_dir / "l2_lenses" / "translate" / "vault_secret_1.yaml").write_text(
        yaml.safe_dump(v_card.model_dump(), allow_unicode=True), encoding="utf-8"
    )
    pub_sp_path.write_text(
        yaml.safe_dump(
            {
                "version": "2.3",
                "always_on_tier_s": True,
                "category_modes": {"general": "all", "custom": "all"},
                "active_collections": ["col_vibe"],
                "collections": {"col_vibe": {"name": "Vibe_codeing", "lens_ids": []}},
                "evergreen_ids": [],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    orig = (
        admin_srv.L2_DIR,
        admin_srv.L3_DIR,
        admin_srv.OVERLAY_PATH,
        admin_srv.QUARANTINE_DIR,
        admin_srv.DIST_DIR,
        admin_srv.META_VAULT_DIR,
        admin_srv.DEFAULT_SHELF_POLICY_PATH,
    )
    try:
        admin_srv.L2_DIR = l2_dir
        admin_srv.L3_DIR = l3_dir
        admin_srv.OVERLAY_PATH = ov_path
        admin_srv.QUARANTINE_DIR = q_dir
        admin_srv.DIST_DIR = dist_dir
        admin_srv.META_VAULT_DIR = vault_dir
        admin_srv.DEFAULT_SHELF_POLICY_PATH = pub_sp_path
        client = TestClient(admin_srv.create_admin_app())

        # Create a brand new card -> should sort #1 under sort=newest
        r_new = client.post(
            "/api/lenses/manual",
            json={
                "title": "最新卡片",
                "tagline": "刚刚创建",
                "primitive": "deconstruct",
                "category": "custom",
                "tier": "A",
                "pain_point": "刚刚遇到的新问题",
                "trigger_utterances": ["最新卡片", "新问题"],
                "content": "请按最新思路拆解。",
            },
        )
        assert r_new.status_code == 200
        new_id = r_new.json()["lens"]["id"]

        newest = client.get("/api/lenses?shelf=personal&sort=newest").json()["items"]
        assert newest[0]["id"] == new_id

        # Single-card add/remove collection + vault anti-leakage guard
        r_add = client.put(f"/api/lenses/{new_id}/collections", json={"add": "col_vibe"})
        assert r_add.status_code == 200
        in_col = client.get("/api/lenses?collection=col_vibe").json()["items"]
        assert new_id in {x["id"] for x in in_col}

        r_leak = client.put("/api/lenses/vault_secret_1/collections", json={"add": "col_vibe"})
        assert r_leak.status_code == 400
        assert "vault_secret_1" not in pub_sp_path.read_text(encoding="utf-8")

        r_rm = client.put(f"/api/lenses/{new_id}/collections", json={"remove": "col_vibe"})
        assert r_rm.status_code == 200
        assert client.get("/api/lenses?collection=col_vibe").json()["items"] == []

        # Rename collection cleanly
        r_ren = client.put("/api/shelf/collections/col_vibe", json={"name": "Vibe_Coding"})
        assert r_ren.status_code == 200
        assert client.get("/api/overview").json()["shelf_policy"]["collections"]["col_vibe"]["name"] == "Vibe_Coding"
    finally:
        (
            admin_srv.L2_DIR,
            admin_srv.L3_DIR,
            admin_srv.OVERLAY_PATH,
            admin_srv.QUARANTINE_DIR,
            admin_srv.DIST_DIR,
            admin_srv.META_VAULT_DIR,
            admin_srv.DEFAULT_SHELF_POLICY_PATH,
        ) = orig


def test_inplace_overlay_fields_persist_in_l3_build_and_safe_upsert(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    import admin.server as admin_srv
    from pipeline.l2_to_l3 import run_l2_to_l3
    from pipeline.schema import L2Lens, LensExecution, LensMeta, LensRouting

    l2_dir = tmp_path / "l2_lenses"
    l3_dir = tmp_path / "l3_production"
    q_dir = tmp_path / "quarantine"
    ov_path = tmp_path / "overrides.yaml"
    dist_dir = tmp_path / "dist"
    (l2_dir / "critique").mkdir(parents=True)
    ov_path.write_text("overrides: {}\n", encoding="utf-8")

    base_lens = L2Lens(
        id="card_refill_test",
        title="旧标题",
        tagline="旧摘要",
        routing=LensRouting(
            primitive="critique",
            goals=["evaluate"],
            clarity_fit=["concrete"],
            domains=["general"],
            pain_point="旧痛点描述文本",
            trigger_utterances=["旧口语一", "旧口语二"],
            anti_triggers=[],
        ),
        execution=LensExecution(
            action_type="inject_prompt",
            inject_mode="wrap",
            content="旧指令正文 {{input}}",
            variables=[],
        ),
        meta=LensMeta(
            source="manual-custom",
            source_ref="manual#refill",
            source_hash=content_sha256("refill"),
            tier="A",
        ),
    )
    (l2_dir / "critique" / "card_refill_test.yaml").write_text(
        yaml.safe_dump(base_lens.model_dump(), allow_unicode=True), encoding="utf-8"
    )

    orig = (
        admin_srv.L2_DIR,
        admin_srv.L3_DIR,
        admin_srv.OVERLAY_PATH,
        admin_srv.QUARANTINE_DIR,
        admin_srv.DIST_DIR,
        admin_srv.META_VAULT_DIR,
    )
    try:
        admin_srv.L2_DIR = l2_dir
        admin_srv.L3_DIR = l3_dir
        admin_srv.OVERLAY_PATH = ov_path
        admin_srv.QUARANTINE_DIR = q_dir
        admin_srv.DIST_DIR = dist_dir
        admin_srv.META_VAULT_DIR = None
        client = TestClient(admin_srv.create_admin_app())

        # Seed prior add/remove trigger overrides and a base_weight
        assert client.put("/api/overlays/card_refill_test", json={"base_weight": 1.4}).status_code == 200
        assert client.post("/api/overlays/card_refill_test/add-trigger", json={"utterance": "历史新增口语"}).status_code == 200
        assert client.post("/api/overlays/card_refill_test/remove-trigger", json={"utterance": "旧口语二"}).status_code == 200

        # Empty PUT must NOT wipe existing overlay
        r_empty = client.put("/api/overlays/card_refill_test", json={})
        assert r_empty.status_code == 200
        assert r_empty.json()["override"].get("base_weight") == 1.4

        # In-place update with full trigger_utterances + anti_triggers + pain_point + content
        r_upd = client.put(
            "/api/overlays/card_refill_test",
            json={
                "pain_point": "新痛点描述文本",
                "content": "新指令正文含【参数】",
                "trigger_utterances": ["新口语一", "新口语二"],
                "anti_triggers": ["无关场景不触发"],
            },
        )
        assert r_upd.status_code == 200
        ov_entry = r_upd.json()["override"]
        assert "add_trigger_utterances" not in ov_entry
        assert "remove_trigger_utterances" not in ov_entry
        assert ov_entry["base_weight"] == 1.4

        # run_l2_to_l3 & build_release must materialize ALL 5 overlay fields into L3 and dist/current
        kept, _, _ = run_l2_to_l3(
            l2_dir=l2_dir,
            l3_dir=l3_dir,
            overlay_path=ov_path,
            quarantine_dir=q_dir,
        )
        assert len(kept) == 1
        k0 = kept[0]
        assert k0.routing.pain_point == "新痛点描述文本"
        assert k0.routing.trigger_utterances == ["新口语一", "新口语二"]
        assert k0.routing.anti_triggers == ["无关场景不触发"]
        assert "{{input}}" in k0.execution.content
        assert "参数" in k0.execution.variables

        build_release(
            l2_dir=l2_dir,
            l3_dir=l3_dir,
            overlay_path=ov_path,
            quarantine_dir=q_dir,
            dist_dir=dist_dir,
            embedder=None,
        )
        published = json.loads((dist_dir / "current" / "lenses.json").read_text(encoding="utf-8"))
        assert published[0]["routing"]["pain_point"] == "新痛点描述文本"
        assert published[0]["routing"]["trigger_utterances"] == ["新口语一", "新口语二"]
        assert published[0]["routing"]["anti_triggers"] == ["无关场景不触发"]
        assert "新指令正文含【参数】" in published[0]["execution"]["content"]
    finally:
        (
            admin_srv.L2_DIR,
            admin_srv.L3_DIR,
            admin_srv.OVERLAY_PATH,
            admin_srv.QUARANTINE_DIR,
            admin_srv.DIST_DIR,
            admin_srv.META_VAULT_DIR,
        ) = orig
