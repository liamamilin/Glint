"""Thin adapters for L1 cognitive material sources:
1. glint_core (local brain-server/data/cards.yaml)
2. Prompt-taxonomy (https://liamamilin.github.io/Prompt-taxonomy/catalog.json)
3. DISCOVERY-TAXONOMY (https://liamamilin.github.io/DISCOVERY-TAXONOMY/data/<arena>.json)
4. SUPER-INDIVIDUAL-TOOLBOX (https://liamamilin.github.io/SUPER-INDIVIDUAL-TOOLBOX/data/manifest.json)
5. Local refined L1 JSON directory (e.g. data/l1_materials/atlas_refined/)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import yaml

from pipeline.schema import L1Material, content_sha256, make_stable_id

DISCOVERY_ARENAS = ["build", "business", "content", "cognition", "career", "life"]


def _zh(val: Any) -> str:
    if isinstance(val, dict):
        return str(val.get("zh") or val.get("en") or "").strip()
    return str(val or "").strip()


def _zh_list(val: Any) -> list[str]:
    if isinstance(val, dict):
        raw = val.get("zh") or val.get("en") or []
    else:
        raw = val or []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [str(raw).strip()] if str(raw).strip() else []


def _save_l1_snapshot(snapshot_dir: Path, materials: list[L1Material], raw_payload: Any = None) -> None:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    if raw_payload is not None:
        (snapshot_dir / "_raw_snapshot.json").write_text(
            json.dumps(raw_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    for mat in materials:
        (snapshot_dir / f"{mat.id}.json").write_text(
            mat.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
    manifest = {"count": len(materials), "ids": [m.id for m in materials]}
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_l1_directory(directory: Path) -> list[L1Material]:
    if not directory.exists():
        return []
    materials: list[L1Material] = []
    for path in sorted(directory.glob("*.json")):
        if path.name in {"manifest.json", "_raw_snapshot.json"}:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        materials.append(L1Material.model_validate(data))
    return materials


# =====================================================================
# 1. Local Glint Core Cards (brain-server/data/cards.yaml)
# =====================================================================

def load_glint_core_l1(cards_yaml_path: Path, snapshot_dir: Path | None = None) -> list[L1Material]:
    raw = yaml.safe_load(cards_yaml_path.read_text(encoding="utf-8")) or {}
    materials: list[L1Material] = []
    for entry in raw.get("cards", []):
        cid = str(entry["id"]).strip()
        title = str(entry["title"]).strip()
        prompt_text = str(entry["prompt"]).strip()
        intent = str(entry.get("intent", "")).strip()
        dim = str(entry.get("dimension", "")).strip()
        mode = str(entry.get("mode", "append")).strip()
        materials.append(
            L1Material(
                id=make_stable_id("core", cid),
                source="glint-core",
                source_ref=f"cards.yaml#{cid}",
                title=title,
                content=prompt_text,
                content_hash=content_sha256(f"{title}\n{intent}\n{prompt_text}"),
                suggested_action_type="inject_prompt",
                domain_hints=[dim] if dim else [],
                problem_solved=intent,
                metadata={"original_id": cid, "legacy_dimension": dim, "legacy_mode": mode},
            )
        )
    if snapshot_dir is not None:
        _save_l1_snapshot(snapshot_dir, materials)
    return materials


# =====================================================================
# 2. Prompt-taxonomy (catalog.json: 224 prompts + 19 workflows)
# =====================================================================

def fetch_prompt_taxonomy_l1(base_url: str, snapshot_dir: Path, timeout_s: float = 20.0) -> list[L1Material]:
    catalog_url = urljoin(base_url.rstrip("/") + "/", "catalog.json")
    with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
        resp = client.get(catalog_url)
        resp.raise_for_status()
        payload = resp.json()

    cat_names = {c["id"]: c.get("name", c["id"]) for c in payload.get("categories", []) if isinstance(c, dict)}
    materials: list[L1Material] = []

    for rec in payload.get("prompts", []):
        raw_id = str(rec.get("id", "")).strip()
        title = str(rec.get("title", raw_id)).strip()
        desc = str(rec.get("description", "")).strip()
        template = str(rec.get("template", "")).strip()
        category = str(rec.get("category", "")).strip()
        tags = [str(t).strip() for t in (rec.get("tags") or []) if str(t).strip()]
        variables = [str(v).strip() for v in (rec.get("variables") or []) if str(v).strip()]
        domains = ([category] if category else []) + tags[:4]

        materials.append(
            L1Material(
                id=make_stable_id("pt", raw_id),
                source="prompt-taxonomy",
                source_ref=f"{catalog_url}#{raw_id}",
                title=title,
                content=template,
                content_hash=content_sha256(f"{title}\n{desc}\n{template}"),
                suggested_action_type="inject_prompt",
                domain_hints=list(dict.fromkeys(domains)),
                problem_solved=desc,
                when_to_use=f"分类: {cat_names.get(category, category)}" if category else "",
                metadata={
                    "original_id": raw_id,
                    "category": category,
                    "variables": variables,
                },
            )
        )

    _save_l1_snapshot(snapshot_dir, materials, raw_payload=payload)
    return materials


# =====================================================================
# 3. DISCOVERY-TAXONOMY (data/<arena>.json: 18 blindspot cards)
# =====================================================================

def fetch_discovery_taxonomy_l1(base_url: str, snapshot_dir: Path, timeout_s: float = 20.0) -> list[L1Material]:
    base = base_url.rstrip("/") + "/"
    raw_by_arena: dict[str, Any] = {}
    materials: list[L1Material] = []

    with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
        for arena in DISCOVERY_ARENAS:
            url = urljoin(base, f"data/{arena}.json")
            resp = client.get(url)
            resp.raise_for_status()
            cards = resp.json()
            raw_by_arena[arena] = cards

            for rec in cards:
                raw_id = str(rec.get("id", "")).strip()
                title = _zh(rec.get("title")) or raw_id
                old_assump = _zh(rec.get("old_assumption"))
                new_shift = _zh(rec.get("new_shift"))
                seed_prompts = _zh_list(rec.get("seed_prompts"))
                lenses = [str(x).strip() for x in (rec.get("lenses") or []) if str(x).strip()]
                tags = [str(x).strip() for x in (rec.get("tags") or []) if str(x).strip()]

                parts = []
                if old_assump:
                    parts.append(f"旧共识（盲区）：{old_assump}")
                if new_shift:
                    parts.append(f"新现实（破盲视角）：{new_shift}")
                if seed_prompts:
                    parts.append("破冰追问母题：\n" + "\n".join(f"- {p}" for p in seed_prompts))
                body = "\n\n".join(parts)

                materials.append(
                    L1Material(
                        id=make_stable_id("disc", raw_id),
                        source="discovery-taxonomy",
                        source_ref=f"{url}#{raw_id}",
                        title=title,
                        content=body,
                        content_hash=content_sha256(f"{title}\n{body}"),
                        suggested_action_type="show_checklist",
                        domain_hints=list(dict.fromkeys([arena] + lenses + tags[:4])),
                        problem_solved=old_assump,
                        when_to_use=new_shift,
                        procedural_steps=seed_prompts,
                        metadata={"original_id": raw_id, "arena": arena, "lenses": lenses},
                    )
                )

    _save_l1_snapshot(snapshot_dir, materials, raw_payload=raw_by_arena)
    return materials


# =====================================================================
# 4. SUPER-INDIVIDUAL-TOOLBOX (data/manifest.json -> 9 drawers, 36 tools)
# =====================================================================

def fetch_super_individual_toolbox_l1(base_url: str, snapshot_dir: Path, timeout_s: float = 20.0) -> list[L1Material]:
    base = base_url.rstrip("/") + "/"
    manifest_url = urljoin(base, "data/manifest.json")
    raw_drawers: dict[str, Any] = {}
    materials: list[L1Material] = []

    with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
        m_resp = client.get(manifest_url)
        m_resp.raise_for_status()
        manifest = m_resp.json()

        for drawer in manifest.get("drawers", []):
            d_file = drawer["file"]
            d_id = drawer["id"]
            d_zh = drawer.get("zh", d_id)
            d_url = urljoin(base, f"data/{d_file}")
            resp = client.get(d_url)
            resp.raise_for_status()
            tools = resp.json()
            raw_drawers[d_id] = tools

            for rec in tools:
                raw_id = str(rec.get("id", "")).strip()
                title = _zh(rec.get("title")) or raw_id
                metaphor = _zh(rec.get("metaphor"))
                trigger = _zh(rec.get("trigger"))
                mechanisms = _zh_list(rec.get("mechanism"))
                deliverable = _zh(rec.get("deliverable"))
                stack = _zh(rec.get("current_stack"))
                anti = _zh(rec.get("anti_pattern"))

                parts = []
                if metaphor:
                    parts.append(f"定位：{metaphor}")
                if trigger:
                    parts.append(f"触发场景：{trigger}")
                if mechanisms:
                    parts.append("核心机制：\n" + "\n".join(f"- {m}" for m in mechanisms))
                if deliverable:
                    parts.append(f"交付物：{deliverable}")
                if stack:
                    parts.append(f"推荐装配：{stack}")
                if anti:
                    parts.append(f"误用警告：{anti}")
                body = "\n\n".join(parts)

                materials.append(
                    L1Material(
                        id=make_stable_id("sit", raw_id),
                        source="super-individual-toolbox",
                        source_ref=f"{d_url}#{raw_id}",
                        title=title,
                        content=body,
                        content_hash=content_sha256(f"{title}\n{body}"),
                        suggested_action_type="open_resource",
                        domain_hints=list(dict.fromkeys([d_id, d_zh])),
                        problem_solved=trigger or metaphor,
                        when_to_use=trigger,
                        procedural_steps=mechanisms,
                        anti_patterns=[anti] if anti else [],
                        metadata={"original_id": raw_id, "drawer": d_id, "drawer_zh": d_zh, "deliverable": deliverable},
                    )
                )

    _save_l1_snapshot(snapshot_dir, materials, raw_payload={"manifest": manifest, "drawers": raw_drawers})
    return materials
