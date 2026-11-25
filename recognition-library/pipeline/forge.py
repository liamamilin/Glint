"""M4 Supply Forge: L0 raw ingestion, Glint Forge zero-raw starter pack, ATLAS topic curation & manual card creation."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import yaml

from pipeline.adapters.atlas_l0 import load_atlas_l0_sample
from pipeline.l0_to_l1 import run_l0_to_l1
from pipeline.l1_to_l2 import compile_l1_to_l2
from pipeline.ontology import LIB_ROOT, load_sources
from pipeline.opencode_client import DEFAULT_MODEL, OpenCodeGoClient
from pipeline.schema import (
    L0RawItem,
    L1Material,
    L2Lens,
    LensExecution,
    LensMeta,
    LensRouting,
    content_sha256,
    make_stable_id,
)

FORGE_PROMPT = """你是 Glint（微光）的领域冷启动引擎（Glint Forge）。
用户提供了一个新领域与高频痛点，请你按 7 大冻结认知元操作（compress, deconstruct, critique, diverge, converge, translate, metacognize）各设计 1 张高杠杆认知透镜（共 7 张）。
要求：
1. 拒绝陈述性百科知识，每张卡必须是可复用的程序性/启发式思考框架；
2. 每张卡有简短标题（<= 12字）、一句话摘要、真实痛点描述、3 句用户碎片口语 `trigger_utterances`、以及可直接追加给强 LLM 的自然第一/第二人称执行指令 `content`。
严格输出单个 JSON 对象（禁止 Markdown 围栏）：
{
  "lenses": [
    {
      "slug": "short_ascii_slug",
      "title": "短标题",
      "tagline": "一句话摘要",
      "primitive": "deconstruct",
      "goals": ["create"],
      "clarity_fit": ["vague", "concrete"],
      "domains": ["domain_tag"],
      "app_affinity": ["Notion", "Cursor"],
      "pain_point": "用户真实痛点",
      "trigger_utterances": ["口语1", "口语2", "口语3"],
      "action_type": "inject_prompt",
      "inject_mode": "append",
      "content": "执行指令正文"
    }
  ]
}
"""


def create_manual_lens(
    *,
    title: str,
    tagline: str,
    primitive: str,
    pain_point: str,
    trigger_utterances: list[str],
    content: str,
    action_type: str = "inject_prompt",
    inject_mode: str = "append",
    goals: list[str] | None = None,
    clarity_fit: list[str] | None = None,
    domains: list[str] | None = None,
    category: str = "custom",
    tier: str = "A",
    l2_dir: Path = LIB_ROOT / "data" / "l2_lenses",
) -> L2Lens:
    """Create a hand-crafted personal lens directly in L2 (ready for L2->L3 build)."""
    lid = make_stable_id("manual", f"{title}_{int(time.time()) % 100000}")
    cleaned_triggers = [t.strip() for t in trigger_utterances if t.strip()]
    if len(cleaned_triggers) < 2:
        for fallback in (title.strip(), pain_point.strip()):
            if fallback and fallback not in cleaned_triggers:
                cleaned_triggers.append(fallback)
    norm_action = action_type if action_type in {"inject_prompt", "show_checklist", "open_resource"} else "inject_prompt"
    lens = L2Lens(
        id=lid,
        title=title.strip(),
        tagline=tagline.strip() or title.strip(),
        routing=LensRouting(
            primitive=primitive,
            goals=goals or ["optimize"],
            clarity_fit=clarity_fit or ["vague", "concrete"],
            domains=domains or [category],
            app_affinity=[],
            pain_point=pain_point.strip(),
            trigger_utterances=cleaned_triggers,
        ),
        execution=LensExecution(
            action_type=norm_action,
            inject_mode=inject_mode if norm_action == "inject_prompt" else None,  # type: ignore[arg-type]
            content=content.strip(),
            variables=[],
        ),
        meta=LensMeta(
            source="manual-custom",
            source_ref=f"manual#{lid}",
            source_hash=content_sha256(f"{title}\n{content}"),
            status="active",
            tier=tier if tier in {"S", "A", "B"} else "A",  # type: ignore[arg-type]
            category=category,
            confidence=1.0,
        ),
    )
    out_path = l2_dir / lens.routing.primitive / f"{lens.id}.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        yaml.safe_dump(lens.model_dump(), allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    return lens


def ingest_l0_raw_text(
    *,
    title: str,
    raw_text: str,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """Channel A: Run pasted L0 document through Gate 1 (L0->L1) -> Gate 2 (L1->L2)."""
    lid = make_stable_id("l0doc", f"{title}_{content_sha256(raw_text)[:8]}")
    item = L0RawItem(
        id=lid,
        source="user-l0-forge",
        source_ref=f"paste#{lid}",
        title=title.strip(),
        raw_text=raw_text.strip()[:8000],
        content_hash=content_sha256(raw_text.strip()),
    )
    llm = OpenCodeGoClient(model=model, cwd=LIB_ROOT)
    l1_out = LIB_ROOT / "data" / "l1_materials" / "forge_refined"
    q_dir = LIB_ROOT / "data" / "quarantine"
    s1 = run_l0_to_l1([item], llm=llm, out_dir=l1_out, quarantine_dir=q_dir, workers=1, force=True)
    if s1.accepted == 0:
        return {"stage": "l0_gate_rejected", "item_id": lid, "l0_summary": s1.__dict__}

    mat_path = l1_out / f"{lid}.json"
    mat = L1Material.model_validate(json.loads(mat_path.read_text(encoding="utf-8")))
    s2 = compile_l1_to_l2(
        [mat],
        llm=llm,
        l2_dir=LIB_ROOT / "data" / "l2_lenses",
        quarantine_dir=q_dir,
        embedder=None,
        workers=1,
        force=True,
    )
    return {"stage": "compiled_to_l2", "item_id": lid, "l0_summary": s1.__dict__, "l2_summary": s2.__dict__}


def forge_starter_domain_pack(
    *,
    domain_name: str,
    pain_points: str,
    style_pref: str = "硬核实战、简洁直击要点",
    category: str = "custom",
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """Channel B: Zero-raw synthesis of a 7-primitive Starter Domain Pack into L2 + custom collection."""
    prompt = (
        f"{FORGE_PROMPT}\n\n"
        f"## 用户三元信号\n"
        f"- 目标领域：{domain_name}\n"
        f"- 高频痛点与期望状态：{pain_points}\n"
        f"- 表达风格偏好：{style_pref}\n"
    )
    llm = OpenCodeGoClient(model=model, cwd=LIB_ROOT)
    resp = llm.call_json(prompt)
    raw_lenses = resp.parsed_json.get("lenses") or []

    created_ids: list[str] = []
    l2_dir = LIB_ROOT / "data" / "l2_lenses"
    for entry in raw_lenses:
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("slug") or entry.get("title") or "card")
        prim = str(entry.get("primitive", "deconstruct")).strip().lower()
        if prim not in {"compress", "deconstruct", "critique", "diverge", "converge", "translate", "metacognize"}:
            prim = "deconstruct"
        lid = make_stable_id("forge", f"{domain_name}_{slug}")
        raw_action = str(entry.get("action_type") or "inject_prompt").strip()
        norm_action = raw_action if raw_action in {"inject_prompt", "show_checklist", "open_resource"} else "inject_prompt"
        norm_inject_mode = ("wrap" if entry.get("inject_mode") == "wrap" else "append") if norm_action == "inject_prompt" else None
        raw_content = str(entry.get("content") or "").strip() or f"请结合{domain_name}视角帮我分析以上内容。"
        if norm_action == "inject_prompt" and norm_inject_mode == "wrap" and "{{input}}" not in raw_content:
            raw_content = raw_content + "\n\n{{input}}"
        lens = L2Lens(
            id=lid,
            title=str(entry.get("title", slug)).strip()[:18],
            tagline=str(entry.get("tagline", "")).strip() or str(entry.get("title", slug)),
            routing=LensRouting(
                primitive=prim,
                goals=[g for g in (entry.get("goals") or ["optimize"]) if g in {"create", "optimize", "debug", "evaluate", "digest"}] or ["optimize"],
                clarity_fit=[c for c in (entry.get("clarity_fit") or ["vague", "concrete"]) if c in {"vague", "concrete", "conflicted"}] or ["vague", "concrete"],
                domains=[str(d).lower() for d in (entry.get("domains") or [domain_name.lower()])],
                app_affinity=[str(a) for a in (entry.get("app_affinity") or [])],
                pain_point=str(entry.get("pain_point") or pain_points).strip(),
                trigger_utterances=[str(t).strip() for t in (entry.get("trigger_utterances") or [domain_name, pain_points[:20]]) if str(t).strip()],
            ),
            execution=LensExecution(
                action_type=norm_action,
                inject_mode=norm_inject_mode,  # type: ignore[arg-type]
                content=raw_content,
                variables=[],
            ),
            meta=LensMeta(
                source=f"forge-{make_stable_id('', domain_name)[:16]}",
                source_ref=f"glint-forge:{domain_name}",
                source_hash=content_sha256(json.dumps(entry, ensure_ascii=False)),
                status="active",
                tier="A",
                category=category,
                confidence=0.88,
            ),
        )
        out_path = l2_dir / prim / f"{lid}.yaml"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            yaml.safe_dump(lens.model_dump(), allow_unicode=True, sort_keys=False, width=120),
            encoding="utf-8",
        )
        created_ids.append(lid)

    return {"domain_name": domain_name, "created_count": len(created_ids), "created_ids": created_ids}


def search_atlas_l0(query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Channel C: Search ATLAS-METHODS SkillRecord JSONL by keyword for selective L3 curation."""
    l0_cfg = load_sources().get("l0_sources", {}).get("atlas_methods", {})
    jsonl_path = Path(l0_cfg.get("path", ""))
    if not jsonl_path.exists():
        return []
    q = query.strip().lower()
    results: list[dict[str, Any]] = []
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for idx, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            content = rec.get("content") or {}
            src = rec.get("source") or {}
            title = str(content.get("title") or content.get("name") or "")
            summary = str(content.get("summary") or "")
            repo = str(src.get("repository") or "")
            hay = f"{title} {summary} {repo}".lower()
            if not q or q in hay:
                results.append(
                    {
                        "offset": idx,
                        "repository": repo,
                        "skill_path": src.get("skill_path", ""),
                        "title": title,
                        "summary": summary[:160],
                    }
                )
                if len(results) >= limit:
                    break
    return results


def import_atlas_offsets(offsets: list[int], model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Selectively run L0->L1->L2 on chosen ATLAS-METHODS record offsets (lands in L3 off_shelf)."""
    l0_cfg = load_sources().get("l0_sources", {}).get("atlas_methods", {})
    jsonl_path = Path(l0_cfg.get("path", ""))
    if not jsonl_path.exists():
        return {"imported": 0, "error": f"ATLAS JSONL not found: {jsonl_path}"}
    items: list[L0RawItem] = []
    for off in sorted(set(offsets)):
        batch = load_atlas_l0_sample(jsonl_path, limit=1, offset=off)
        items.extend(batch)
    if not items:
        return {"imported": 0, "error": "No matching ATLAS records found"}

    llm = OpenCodeGoClient(model=model, cwd=LIB_ROOT)
    l1_out = LIB_ROOT / "data" / "l1_materials" / "atlas_refined"
    q_dir = LIB_ROOT / "data" / "quarantine"
    s1 = run_l0_to_l1(items, llm=llm, out_dir=l1_out, quarantine_dir=q_dir, workers=2)

    mats: list[L1Material] = []
    for it in items:
        p = l1_out / f"{it.id}.json"
        if p.exists():
            mats.append(L1Material.model_validate(json.loads(p.read_text(encoding="utf-8"))))

    if not mats:
        return {"l0_summary": s1.__dict__, "l2_summary": None}

    s2 = compile_l1_to_l2(
        mats,
        llm=llm,
        l2_dir=LIB_ROOT / "data" / "l2_lenses",
        quarantine_dir=q_dir,
        embedder=None,
        workers=2,
    )
    return {"l0_summary": s1.__dict__, "l2_summary": s2.__dict__}
