"""Unified Glint Management Console API (L3/L4 Shelf, M3 Evolution Flywheel, M4 Forge, Eval & Policy)."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from pipeline.builder import (
    apply_overlays,
    build_release,
    load_overlay,
    rollback_release,
)
from pipeline.embed_client import DEFAULT_EMBED_MODEL, OllamaEmbedClient
from pipeline.evolution import (
    DEFAULT_USAGE_DB,
    compute_shelf_proposals,
    compute_usage_analytics,
    export_sft_dataset,
    synthesize_draft_from_friction,
)
from pipeline.forge import (
    create_manual_lens,
    forge_starter_domain_pack,
    import_atlas_offsets,
    ingest_l0_raw_text,
    search_atlas_l0,
)
from pipeline.l1_to_l2 import load_existing_l2_lenses
from pipeline.l2_to_l3 import (
    DEFAULT_CLEANING_RULES_PATH,
    DEFAULT_SHELF_POLICY_PATH,
    DEFAULT_TAXONOMY_PATH,
    load_shelf_policy,
    load_taxonomy,
)
from pipeline.ontology import DEFAULT_POLICY_PATH, LIB_ROOT, Ontology, load_policy, load_sources
from pipeline.schema import LensOverride, make_stable_id

ADMIN_DIR = Path(__file__).resolve().parent
L2_DIR = LIB_ROOT / "data" / "l2_lenses"
L3_DIR = LIB_ROOT / "data" / "l3_production"
OVERLAY_PATH = LIB_ROOT / "data" / "overlays" / "overrides.yaml"
QUARANTINE_DIR = LIB_ROOT / "data" / "quarantine"
DIST_DIR = LIB_ROOT / "dist"

CONFIG_FILES = {
    "routing_policy": DEFAULT_POLICY_PATH,
    "cleaning_rules": DEFAULT_CLEANING_RULES_PATH,
    "shelf_policy": DEFAULT_SHELF_POLICY_PATH,
    "taxonomy": DEFAULT_TAXONOMY_PATH,
}


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    app_name: str = ""
    primitive: str | None = None
    top_k: int = Field(default=6, ge=1, le=20)


class RollbackRequest(BaseModel):
    version: str | None = None


class CategoryModeRequest(BaseModel):
    category: str
    mode: str = Field(..., pattern="^(off|tier_a|all)$")


class CollectionCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    lens_ids: list[str] = Field(default_factory=list)
    active: bool = True


class CollectionToggleRequest(BaseModel):
    active: bool


class BatchOverlayRequest(BaseModel):
    lens_ids: list[str] = Field(..., min_length=1)
    shelf: str | None = None
    tier: str | None = None
    category: str | None = None
    add_collection: str | None = None


class ManualLensRequest(BaseModel):
    title: str = Field(..., min_length=2)
    tagline: str = ""
    primitive: str = "deconstruct"
    pain_point: str = Field(..., min_length=4)
    trigger_utterances: list[str] = Field(..., min_length=1)
    content: str = Field(..., min_length=4)
    action_type: str = "inject_prompt"
    inject_mode: str = "append"
    category: str = "custom"
    tier: str = "A"


class L0IngestRequest(BaseModel):
    title: str = Field(..., min_length=2)
    raw_text: str = Field(..., min_length=20)


class ForgePackRequest(BaseModel):
    domain_name: str = Field(..., min_length=2)
    pain_points: str = Field(..., min_length=4)
    style_pref: str = "硬核实战、简洁直击要点"
    category: str = "custom"
    auto_activate_collection: bool = True


class AtlasImportRequest(BaseModel):
    offsets: list[int] = Field(..., min_length=1, max_length=20)


class FrictionDraftRequest(BaseModel):
    custom_intent: str = Field(..., min_length=3)
    sample_input: str = ""


class ConfigUpdateRequest(BaseModel):
    yaml_text: str = Field(..., min_length=2)


class InspectRecallRequest(BaseModel):
    query: str = Field(..., min_length=1)
    app_name: str = "Unknown"
    top_k: int = Field(default=20, ge=1, le=50)


class AddTriggerRequest(BaseModel):
    utterance: str = Field(..., min_length=2)


class CognitiveGapCreateRequest(BaseModel):
    query: str = Field(..., min_length=2)
    note: str = ""
    app_name: str = "Unknown"


class CognitiveGapUpdateRequest(BaseModel):
    status: str = Field(..., pattern="^(open|resolved)$")


GAPS_PATH = LIB_ROOT / "data" / "overlays" / "cognitive_gaps.yaml"
_CONFIG_LOCK = threading.Lock()
_BUILD_LOCK = threading.Lock()


def _load_gaps() -> list[dict[str, Any]]:
    if not GAPS_PATH.exists():
        return []
    raw = yaml.safe_load(GAPS_PATH.read_text(encoding="utf-8")) or {}
    return list(raw.get("gaps") or [])


def _save_gaps(gaps: list[dict[str, Any]]) -> None:
    _save_yaml(GAPS_PATH, {"gaps": gaps})


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}"
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def _save_yaml(path: Path, data: dict[str, Any]) -> None:
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)
    with _CONFIG_LOCK:
        _atomic_write_text(path, text)


def _save_overlay_file(overlay_path: Path, overrides: dict[str, dict[str, Any]]) -> None:
    cleaned = {k: v for k, v in sorted(overrides.items()) if v}
    _save_yaml(overlay_path, {"overrides": cleaned})


def _count_l1_snapshots() -> dict[str, int]:
    counts: dict[str, int] = {}
    sources_cfg = load_sources().get("l1_sources", {})
    for key, cfg in sources_cfg.items():
        snap_dir = Path(cfg["snapshot_dir"])
        if not snap_dir.is_absolute():
            snap_dir = (LIB_ROOT / snap_dir).resolve()
        manifest = snap_dir / "manifest.json"
        if manifest.exists():
            try:
                counts[key] = int(json.loads(manifest.read_text(encoding="utf-8")).get("count", 0))
            except Exception:
                counts[key] = 0
        else:
            counts[key] = 0
    for extra in ("atlas_refined", "forge_refined"):
        d = LIB_ROOT / "data" / "l1_materials" / extra
        counts[extra] = len([p for p in d.glob("*.json") if p.name != "manifest.json"]) if d.exists() else 0
    return counts


def _load_display_lenses_cached(cache: dict[str, Any]) -> tuple[list[Any], Any]:
    report_path = L3_DIR / "cleaning_report.json"
    r_mtime = report_path.stat().st_mtime_ns if report_path.exists() else -1
    o_mtime = OVERLAY_PATH.stat().st_mtime_ns if OVERLAY_PATH.exists() else -1
    s_mtime = DEFAULT_SHELF_POLICY_PATH.stat().st_mtime_ns if DEFAULT_SHELF_POLICY_PATH.exists() else -1
    l2_mtime = L2_DIR.stat().st_mtime_ns if L2_DIR.exists() else -1
    cache_key = (r_mtime, o_mtime, s_mtime, l2_mtime)
    if cache.get("key") == cache_key and cache.get("merged") is not None:
        return cache["merged"], cache["overlay"]

    ont = Ontology.load()
    overlay = load_overlay(OVERLAY_PATH)
    sp = load_shelf_policy()
    l3_map = load_existing_l2_lenses(L3_DIR) if L3_DIR.exists() else {}
    l2_map = load_existing_l2_lenses(L2_DIR)
    combined = dict(l2_map)
    combined.update(l3_map)
    merged = apply_overlays(combined, overlay, ont)

    # Re-evaluate live declarative shelf & collection membership from shelf_policy.yaml
    # so category_modes / collections toggles immediately preview in the UI before build.
    collections_cfg = sp.get("collections") or {}
    active_collections = set(sp.get("active_collections") or [])
    category_modes = sp.get("category_modes") or {}
    off_shelf_sources = set(sp.get("off_shelf_sources") or [])
    always_on_s = bool(sp.get("always_on_tier_s", True))
    for lens in merged:
        ov = overlay.overrides.get(lens.id)
        member_cols = [
            cid for cid, cinfo in collections_cfg.items()
            if isinstance(cinfo, dict) and lens.id in (cinfo.get("lens_ids") or [])
        ]
        if ov is not None and ov.collections is not None:
            member_cols = list(ov.collections)
        lens.meta.collections = sorted(set(member_cols))

        if ov is not None and ov.shelf is not None:
            lens.meta.shelf = ov.shelf
        elif lens.meta.tier == "S" and always_on_s:
            lens.meta.shelf = "evergreen"
        elif any(c in active_collections for c in lens.meta.collections):
            lens.meta.shelf = "on_shelf"
        elif lens.meta.source in off_shelf_sources:
            lens.meta.shelf = "off_shelf"
        else:
            mode = str(category_modes.get(lens.meta.category, "all"))
            if mode == "all":
                lens.meta.shelf = "on_shelf"
            elif mode == "tier_a" and lens.meta.tier in {"S", "A"}:
                lens.meta.shelf = "on_shelf"
            else:
                lens.meta.shelf = "off_shelf"

    cache["key"] = cache_key
    cache["merged"] = merged
    cache["overlay"] = overlay
    return merged, overlay


class _JobStore:
    """In-memory background job tracker for async M3/M4 LLM pipelines."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def start(self, kind: str, label: str, fn: Any) -> dict[str, Any]:
        job_id = f"{kind}_{uuid.uuid4().hex[:8]}"
        rec = {
            "id": job_id,
            "kind": kind,
            "label": label,
            "status": "running",
            "started_at": time.strftime("%H:%M:%S"),
            "finished_at": None,
            "result": None,
            "error": None,
        }
        with self._lock:
            self._jobs[job_id] = rec

        def _runner() -> None:
            try:
                res = fn()
                with self._lock:
                    self._jobs[job_id]["status"] = "done"
                    self._jobs[job_id]["result"] = res
                    self._jobs[job_id]["finished_at"] = time.strftime("%H:%M:%S")
            except Exception as exc:
                with self._lock:
                    self._jobs[job_id]["status"] = "failed"
                    self._jobs[job_id]["error"] = str(exc)
                    self._jobs[job_id]["finished_at"] = time.strftime("%H:%M:%S")

        threading.Thread(target=_runner, daemon=True).start()
        return rec

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(list(self._jobs.values())))[:30]


def create_admin_app(router_skill: Any = None) -> FastAPI:
    app = FastAPI(title="Glint Management Console", version="0.3.1")
    lens_cache: dict[str, Any] = {}
    jobs = _JobStore()

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((ADMIN_DIR / "index.html").read_text(encoding="utf-8"))

    # =====================================================================
    # 1. Overview & Build / Rollback
    # =====================================================================

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        ont = Ontology.load()
        taxonomy = load_taxonomy()
        shelf_policy = load_shelf_policy()
        merged, overlay = _load_display_lenses_cached(lens_cache)

        by_primitive = {p: 0 for p in sorted(ont.primitives)}
        by_source: Counter[str] = Counter()
        by_shelf: Counter[str] = Counter()
        by_tier: Counter[str] = Counter()
        by_category_l3: Counter[str] = Counter()
        by_category_l4: Counter[str] = Counter()
        by_subcat_l3: Counter[str] = Counter()

        for lens in merged:
            if lens.meta.status == "deprecated":
                by_shelf["deprecated"] += 1
                continue
            by_shelf[lens.meta.shelf] += 1
            by_tier[lens.meta.tier] += 1
            by_category_l3[lens.meta.category] += 1
            by_subcat_l3[f"{lens.meta.category}/{lens.meta.subcategory}"] += 1
            if lens.meta.shelf in {"evergreen", "on_shelf"}:
                by_primitive[lens.routing.primitive] = by_primitive.get(lens.routing.primitive, 0) + 1
                by_source[lens.meta.source] += 1
                by_category_l4[lens.meta.category] += 1

        q_l2 = len(list(QUARANTINE_DIR.glob("*.yaml"))) if QUARANTINE_DIR.exists() else 0
        q_l3_rules = len(list((QUARANTINE_DIR / "l3_rules").glob("*.yaml"))) if (QUARANTINE_DIR / "l3_rules").exists() else 0
        q_l3_dedup = len(list((QUARANTINE_DIR / "l3_dedup").glob("*.yaml"))) if (QUARANTINE_DIR / "l3_dedup").exists() else 0
        q_l0_dir = QUARANTINE_DIR / "l0_rejected"
        q_l0 = len(list(q_l0_dir.glob("*.json"))) if q_l0_dir.exists() else 0

        cleaning_report = None
        report_path = L3_DIR / "cleaning_report.json"
        if report_path.exists():
            cleaning_report = json.loads(report_path.read_text(encoding="utf-8"))

        current_manifest = None
        manifest_path = DIST_DIR / "current" / "manifest.json"
        if manifest_path.exists():
            current_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        releases_dir = DIST_DIR / "releases"
        releases = (
            sorted([p.name for p in releases_dir.iterdir() if p.is_dir() and not p.name.startswith(".")], reverse=True)
            if releases_dir.exists()
            else []
        )

        return {
            "ontology": {
                "schema_version": ont.schema_version,
                "primitives": {
                    k: {"label": v.get("label", k), "transform": v.get("transform", "")}
                    for k, v in (ont.raw.get("primitives") or {}).items()
                },
            },
            "taxonomy": taxonomy.get("categories") or {},
            "shelf_policy": {
                "always_on_tier_s": shelf_policy.get("always_on_tier_s", True),
                "category_modes": shelf_policy.get("category_modes") or {},
                "active_collections": shelf_policy.get("active_collections") or [],
                "collections": shelf_policy.get("collections") or {},
            },
            "l1_counts": _count_l1_snapshots(),
            "l3_total": len(merged),
            "l4_total": by_shelf["evergreen"] + by_shelf["on_shelf"],
            "by_shelf": dict(by_shelf),
            "by_tier": dict(by_tier),
            "by_category_l3": dict(by_category_l3),
            "by_category_l4": dict(by_category_l4),
            "by_subcat_l3": dict(by_subcat_l3),
            "l2_by_primitive": by_primitive,
            "l2_by_source": dict(sorted(by_source.items())),
            "overlays_count": len(overlay.overrides),
            "quarantined_l2": q_l2 + q_l3_rules + q_l3_dedup,
            "rejected_l0": q_l0,
            "l3_cleaning_report": cleaning_report,
            "current_release": current_manifest,
            "releases": releases,
        }

    # =====================================================================
    # 2. L3 Inventory & L4 Shelf Management (Hierarchical + Collections + Batch)
    # =====================================================================

    @app.get("/api/lenses")
    def list_lenses(
        q: str = "",
        primitive: str = "",
        source: str = "",
        clarity: str = "",
        action_type: str = "",
        status: str = "",
        shelf: str = "",
        tier: str = "",
        category: str = "",
        subcategory: str = "",
        collection: str = "",
    ) -> dict[str, Any]:
        merged, overlay = _load_display_lenses_cached(lens_cache)
        q_lower = q.strip().lower()
        items: list[dict[str, Any]] = []
        for lens in merged:
            if primitive and lens.routing.primitive != primitive:
                continue
            if source and lens.meta.source != source:
                continue
            if clarity and clarity not in lens.routing.clarity_fit:
                continue
            if action_type and lens.execution.action_type != action_type:
                continue
            if status and lens.meta.status != status:
                continue
            if shelf == "l4_all":
                if lens.meta.status != "active" or lens.meta.shelf not in {"evergreen", "on_shelf"}:
                    continue
            elif shelf and lens.meta.shelf != shelf:
                continue
            if tier and lens.meta.tier != tier:
                continue
            if category and lens.meta.category != category:
                continue
            if subcategory and lens.meta.subcategory != subcategory:
                continue
            if collection and collection not in lens.meta.collections:
                continue
            if q_lower:
                hay = " ".join(
                    [
                        lens.id,
                        lens.title,
                        lens.tagline,
                        lens.routing.pain_point,
                        " ".join(lens.routing.trigger_utterances),
                        " ".join(lens.routing.domains),
                        lens.execution.content,
                    ]
                ).lower()
                if q_lower not in hay:
                    continue

            ov = overlay.overrides.get(lens.id)
            row = lens.model_dump()
            row["has_override"] = ov is not None
            row["override"] = ov.model_dump(exclude_none=True) if ov is not None else {}
            items.append(row)

        return {"total": len(items), "items": items}

    @app.put("/api/overlays/{lens_id}")
    def upsert_overlay(lens_id: str, payload: LensOverride) -> dict[str, Any]:
        ont = Ontology.load()
        l2_map = load_existing_l2_lenses(L2_DIR)
        if lens_id not in l2_map:
            raise HTTPException(status_code=404, detail=f"Lens '{lens_id}' not found")
        if payload.primitive is not None and ont.normalize_primitive(payload.primitive) is None:
            raise HTTPException(status_code=400, detail=f"Invalid primitive '{payload.primitive}'")

        overlay = load_overlay(OVERLAY_PATH)
        raw_map = {k: v.model_dump(exclude_none=True) for k, v in overlay.overrides.items()}
        new_fields = payload.model_dump(exclude_none=True)
        if new_fields:
            merged_entry = dict(raw_map.get(lens_id, {}))
            merged_entry.update(new_fields)
            raw_map[lens_id] = merged_entry
        else:
            raw_map.pop(lens_id, None)
        _save_overlay_file(OVERLAY_PATH, raw_map)
        lens_cache.clear()
        return {"ok": True, "lens_id": lens_id, "override": raw_map.get(lens_id, {})}

    @app.delete("/api/overlays/{lens_id}")
    def delete_overlay(lens_id: str) -> dict[str, Any]:
        overlay = load_overlay(OVERLAY_PATH)
        raw_map = {k: v.model_dump(exclude_none=True) for k, v in overlay.overrides.items()}
        raw_map.pop(lens_id, None)
        _save_overlay_file(OVERLAY_PATH, raw_map)
        lens_cache.clear()
        return {"ok": True, "lens_id": lens_id}

    @app.post("/api/overlays/{lens_id}/add-trigger")
    def add_trigger_overlay(lens_id: str, req: AddTriggerRequest) -> dict[str, Any]:
        l2_map = load_existing_l2_lenses(L2_DIR)
        if lens_id not in l2_map:
            raise HTTPException(status_code=404, detail=f"Lens '{lens_id}' not found")
        overlay = load_overlay(OVERLAY_PATH)
        raw_map = {k: v.model_dump(exclude_none=True) for k, v in overlay.overrides.items()}
        entry = dict(raw_map.get(lens_id, {}))
        existing = list(entry.get("add_trigger_utterances") or [])
        utt = req.utterance.strip()
        if utt and utt not in existing:
            existing.append(utt)
        entry["add_trigger_utterances"] = existing
        raw_map[lens_id] = entry
        _save_overlay_file(OVERLAY_PATH, raw_map)
        lens_cache.clear()
        return {"ok": True, "lens_id": lens_id, "add_trigger_utterances": existing}

    @app.post("/api/inspect-recall")
    async def api_inspect_recall(req: InspectRecallRequest) -> dict[str, Any]:
        if router_skill is not None and hasattr(router_skill, "inspect_recall"):
            return await router_skill.inspect_recall(
                query=req.query,
                app_name=req.app_name,
                top_k=req.top_k,
            )
        # Fallback when admin runs standalone without brain-server router_skill
        sres = semantic_search(SearchRequest(query=req.query, top_k=req.top_k))
        ranked = []
        for idx, item in enumerate(sres["results"], start=1):
            l = item["lens"]
            ranked.append(
                {
                    "rank": idx,
                    "id": l["id"],
                    "title": l["title"],
                    "tagline": l.get("tagline", ""),
                    "primitive": l["routing"]["primitive"],
                    "category": l["meta"].get("category", "general"),
                    "tier": l["meta"].get("tier", "B"),
                    "shelf": l["meta"].get("shelf", "on_shelf"),
                    "source": l["meta"].get("source", ""),
                    "manual_off_shelf": False,
                    "cosine": item["score"],
                    "bm25": 0.0,
                    "rerank_logit": None,
                    "final_score": item["score"],
                    "in_live_top3": idx <= 3,
                    "breakthrough_status": "l4",
                    "breakthrough_gap_top1": None,
                    "breakthrough_gap_top3": None,
                    "trigger_utterances": l["routing"].get("trigger_utterances") or [],
                }
            )
        return {
            "query": req.query.strip(),
            "cleaned_query": req.query.strip(),
            "release_version": None,
            "reranker_active": False,
            "breakthrough_policy": {},
            "would_show_top3": [],
            "ranked_all": ranked,
        }

    @app.get("/api/gaps")
    def api_list_gaps() -> dict[str, Any]:
        gaps = _load_gaps()
        open_gaps = sorted([g for g in gaps if g.get("status") != "resolved"], key=lambda g: g.get("created_at", ""), reverse=True)
        done_gaps = sorted([g for g in gaps if g.get("status") == "resolved"], key=lambda g: g.get("created_at", ""), reverse=True)
        return {"gaps": open_gaps + done_gaps}

    @app.post("/api/gaps")
    def api_create_gap(req: CognitiveGapCreateRequest) -> dict[str, Any]:
        gaps = _load_gaps()
        gap = {
            "id": f"gap_{uuid.uuid4().hex[:8]}",
            "query": req.query.strip(),
            "note": req.note.strip(),
            "app_name": req.app_name,
            "status": "open",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        gaps.insert(0, gap)
        _save_gaps(gaps)
        return {"ok": True, "gap": gap}

    @app.put("/api/gaps/{gap_id}")
    def api_update_gap(gap_id: str, req: CognitiveGapUpdateRequest) -> dict[str, Any]:
        gaps = _load_gaps()
        for g in gaps:
            if g.get("id") == gap_id:
                g["status"] = req.status
                _save_gaps(gaps)
                return {"ok": True, "gap": g}
        raise HTTPException(status_code=404, detail=f"Gap '{gap_id}' not found")

    @app.delete("/api/gaps/{gap_id}")
    def api_delete_gap(gap_id: str) -> dict[str, Any]:
        gaps = [g for g in _load_gaps() if g.get("id") != gap_id]
        _save_gaps(gaps)
        return {"ok": True, "id": gap_id}

    @app.post("/api/overlays/batch")
    def batch_overlay(req: BatchOverlayRequest) -> dict[str, Any]:
        overlay = load_overlay(OVERLAY_PATH)
        raw_map = {k: v.model_dump(exclude_none=True) for k, v in overlay.overrides.items()}
        for lid in req.lens_ids:
            entry = dict(raw_map.get(lid, {}))
            if req.shelf is not None:
                entry["shelf"] = req.shelf
            if req.tier is not None:
                entry["tier"] = req.tier
                if req.tier == "S":
                    entry["scope"] = "universal"
            if req.category is not None:
                entry["category"] = req.category
            if entry:
                raw_map[lid] = entry
        _save_overlay_file(OVERLAY_PATH, raw_map)

        if req.add_collection:
            sp = load_shelf_policy()
            cols = sp.setdefault("collections", {})
            if req.add_collection in cols:
                existing_ids = list(cols[req.add_collection].get("lens_ids") or [])
                cols[req.add_collection]["lens_ids"] = sorted(set(existing_ids + req.lens_ids))
                _save_yaml(DEFAULT_SHELF_POLICY_PATH, sp)

        lens_cache.clear()
        return {"ok": True, "updated": len(req.lens_ids)}

    @app.put("/api/shelf/category-mode")
    def set_category_mode(req: CategoryModeRequest) -> dict[str, Any]:
        sp = load_shelf_policy()
        sp.setdefault("category_modes", {})[req.category] = req.mode
        _save_yaml(DEFAULT_SHELF_POLICY_PATH, sp)
        lens_cache.clear()
        return {"ok": True, "category": req.category, "mode": req.mode}

    @app.post("/api/shelf/collections")
    def create_collection(req: CollectionCreateRequest) -> dict[str, Any]:
        sp = load_shelf_policy()
        cid = make_stable_id("col", req.name)
        sp.setdefault("collections", {})[cid] = {
            "name": req.name.strip(),
            "description": req.description.strip(),
            "lens_ids": sorted(set(req.lens_ids)),
        }
        active_cols = list(sp.get("active_collections") or [])
        if req.active and cid not in active_cols:
            active_cols.append(cid)
        sp["active_collections"] = active_cols
        _save_yaml(DEFAULT_SHELF_POLICY_PATH, sp)
        lens_cache.clear()
        return {"ok": True, "id": cid}

    @app.put("/api/shelf/collections/{cid}/toggle")
    def toggle_collection(cid: str, req: CollectionToggleRequest) -> dict[str, Any]:
        sp = load_shelf_policy()
        if cid not in (sp.get("collections") or {}):
            raise HTTPException(status_code=404, detail=f"Collection '{cid}' not found")
        active_cols = list(sp.get("active_collections") or [])
        if req.active and cid not in active_cols:
            active_cols.append(cid)
        elif not req.active and cid in active_cols:
            active_cols.remove(cid)
        sp["active_collections"] = active_cols
        _save_yaml(DEFAULT_SHELF_POLICY_PATH, sp)
        lens_cache.clear()
        return {"ok": True, "id": cid, "active": req.active}

    @app.delete("/api/shelf/collections/{cid}")
    def delete_collection(cid: str) -> dict[str, Any]:
        sp = load_shelf_policy()
        (sp.get("collections") or {}).pop(cid, None)
        sp["active_collections"] = [c for c in (sp.get("active_collections") or []) if c != cid]
        _save_yaml(DEFAULT_SHELF_POLICY_PATH, sp)
        lens_cache.clear()
        return {"ok": True, "id": cid}

    @app.post("/api/lenses/manual")
    def api_create_manual_lens(req: ManualLensRequest) -> dict[str, Any]:
        lens = create_manual_lens(
            title=req.title,
            tagline=req.tagline,
            primitive=req.primitive,
            pain_point=req.pain_point,
            trigger_utterances=req.trigger_utterances,
            content=req.content,
            action_type=req.action_type,
            inject_mode=req.inject_mode,
            category=req.category,
            tier=req.tier,
            l2_dir=L2_DIR,
        )
        lens_cache.clear()
        return {"ok": True, "lens": lens.model_dump()}

    @app.get("/api/quarantine")
    def list_quarantine() -> dict[str, Any]:
        l2_items: list[dict[str, Any]] = []
        if QUARANTINE_DIR.exists():
            for path in sorted(QUARANTINE_DIR.glob("*.yaml")):
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    l2_items.append(data)
            for sub in ("l3_rules", "l3_dedup"):
                sub_dir = QUARANTINE_DIR / sub
                if sub_dir.exists():
                    for path in sorted(sub_dir.glob("*.yaml")):
                        data = yaml.safe_load(path.read_text(encoding="utf-8"))
                        if isinstance(data, dict):
                            l2_items.append(data)

        l0_items: list[dict[str, Any]] = []
        l0_dir = QUARANTINE_DIR / "l0_rejected"
        if l0_dir.exists():
            for path in sorted(l0_dir.glob("*.json")):
                try:
                    l0_items.append(json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
        return {"l2_quarantined": l2_items, "l0_rejected": l0_items}

    @app.post("/api/build")
    def trigger_build() -> dict[str, Any]:
        if not _BUILD_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="已有清洗发布任务正在进行中，请稍候…")
        try:
            policy = load_policy()
            embed_model = policy.get("models", {}).get("embedding_model", DEFAULT_EMBED_MODEL)
            ollama_url = policy.get("models", {}).get("ollama_base_url", "http://127.0.0.1:11434")
            client = OllamaEmbedClient(model=embed_model, base_url=ollama_url)
            embedder = client if client.is_available() else None
            try:
                release_dir, manifest = build_release(
                    l2_dir=L2_DIR,
                    l3_dir=L3_DIR,
                    overlay_path=OVERLAY_PATH,
                    quarantine_dir=QUARANTINE_DIR,
                    dist_dir=DIST_DIR,
                    embedder=embedder,
                )
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            lens_cache.clear()
            return {"ok": True, "release_dir": str(release_dir), "manifest": manifest.model_dump()}
        finally:
            _BUILD_LOCK.release()

    @app.post("/api/rollback")
    def trigger_rollback(req: RollbackRequest) -> dict[str, Any]:
        try:
            chosen = rollback_release(DIST_DIR, target_version=req.version)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "active_version": chosen}

    # =====================================================================
    # 3. M3 Evolution Flywheel (Analytics, Shelf Proposals, Friction, SFT Export)
    # =====================================================================

    @app.get("/api/evolution/analytics")
    def api_evolution_analytics(days: int = 30) -> dict[str, Any]:
        return compute_usage_analytics(db_path=DEFAULT_USAGE_DB, l3_dir=L3_DIR, days=days)

    @app.get("/api/evolution/proposals")
    def api_evolution_proposals(days: int = 30, min_imp: int = 5) -> dict[str, Any]:
        return compute_shelf_proposals(
            db_path=DEFAULT_USAGE_DB,
            l3_dir=L3_DIR,
            overlay_path=OVERLAY_PATH,
            days=days,
            min_impressions_demote=min_imp,
        )

    @app.post("/api/evolution/friction-to-draft")
    def api_friction_to_draft(req: FrictionDraftRequest) -> dict[str, Any]:
        job = jobs.start(
            "friction_draft",
            f"摩擦转草稿: {req.custom_intent[:24]}",
            lambda: synthesize_draft_from_friction(
                custom_intent=req.custom_intent,
                sample_input=req.sample_input,
            ),
        )
        return {"ok": True, "job": job}

    @app.get("/api/evolution/export-sft")
    def api_export_sft(days: int = 180) -> PlainTextResponse:
        rows = export_sft_dataset(db_path=DEFAULT_USAGE_DB, days=days)
        body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
        return PlainTextResponse(
            body,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="glint_sft_dataset.jsonl"'},
        )

    # =====================================================================
    # 4. M4 Supply Forge (L0 Paste, Zero-Raw Starter Pack, ATLAS Curation, Jobs)
    # =====================================================================

    @app.get("/api/jobs")
    def api_list_jobs() -> dict[str, Any]:
        return {"jobs": jobs.list_all()}

    @app.post("/api/forge/l0-text")
    def api_forge_l0(req: L0IngestRequest) -> dict[str, Any]:
        job = jobs.start(
            "l0_ingest",
            f"L0 投喂精炼: {req.title[:24]}",
            lambda: ingest_l0_raw_text(title=req.title, raw_text=req.raw_text),
        )
        return {"ok": True, "job": job}

    @app.post("/api/forge/starter-pack")
    def api_forge_starter_pack(req: ForgePackRequest) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            res = forge_starter_domain_pack(
                domain_name=req.domain_name,
                pain_points=req.pain_points,
                style_pref=req.style_pref,
                category=req.category,
            )
            if res.get("created_ids"):
                sp = load_shelf_policy()
                cid = make_stable_id("forge", req.domain_name)
                sp.setdefault("collections", {})[cid] = {
                    "name": f"Forge · {req.domain_name}",
                    "description": req.pain_points[:120],
                    "lens_ids": res["created_ids"],
                }
                if req.auto_activate_collection:
                    active_cols = list(sp.get("active_collections") or [])
                    if cid not in active_cols:
                        active_cols.append(cid)
                    sp["active_collections"] = active_cols
                _save_yaml(DEFAULT_SHELF_POLICY_PATH, sp)
                lens_cache.clear()
                res["collection_id"] = cid
            return res

        job = jobs.start("forge_pack", f"Glint Forge 合成: {req.domain_name}", _run)
        return {"ok": True, "job": job}

    @app.get("/api/forge/atlas-search")
    def api_atlas_search(q: str = "", limit: int = 25) -> dict[str, Any]:
        return {"items": search_atlas_l0(query=q, limit=min(limit, 50))}

    @app.post("/api/forge/atlas-import")
    def api_atlas_import(req: AtlasImportRequest) -> dict[str, Any]:
        job = jobs.start(
            "atlas_import",
            f"ATLAS 精选导入 ({len(req.offsets)} 条)",
            lambda: import_atlas_offsets(req.offsets),
        )
        return {"ok": True, "job": job}

    # =====================================================================
    # 5. Sandbox Search & Golden Eval
    # =====================================================================

    @app.post("/api/search")
    def semantic_search(req: SearchRequest) -> dict[str, Any]:
        current_lenses_path = DIST_DIR / "current" / "lenses.json"
        current_emb_path = DIST_DIR / "current" / "embeddings.npz"
        if not current_lenses_path.exists():
            raise HTTPException(status_code=400, detail="No dist/current release built yet.")

        lenses_list: list[dict[str, Any]] = json.loads(current_lenses_path.read_text(encoding="utf-8"))
        by_id = {item["id"]: item for item in lenses_list}

        policy = load_policy()
        embed_model = policy.get("models", {}).get("embedding_model", DEFAULT_EMBED_MODEL)
        ollama_url = policy.get("models", {}).get("ollama_base_url", "http://127.0.0.1:11434")
        client = OllamaEmbedClient(model=embed_model, base_url=ollama_url)

        mode = "keyword_fallback"
        scored: list[tuple[float, dict[str, Any]]] = []
        if current_emb_path.exists() and client.is_available():
            npz = np.load(current_emb_path, allow_pickle=False)
            ids = [str(x) for x in npz["ids"].tolist()]
            matrix = np.asarray(npz["vectors"], dtype=np.float32)
            q_vec = client.embed_texts([req.query])
            sims = (matrix @ q_vec.T).reshape(-1)
            mode = f"bge-m3 ({embed_model})"
            for idx, cid in enumerate(ids):
                lens = by_id.get(cid)
                if not lens:
                    continue
                if req.primitive and lens["routing"]["primitive"] != req.primitive:
                    continue
                scored.append((float(sims[idx]), lens))
        else:
            for lens in lenses_list:
                if req.primitive and lens["routing"]["primitive"] != req.primitive:
                    continue
                hay = f"{lens['title']} {lens['tagline']} {lens['routing']['pain_point']}".lower()
                if req.query.lower() in hay:
                    scored.append((1.0, lens))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return {"mode": mode, "results": [{"score": round(s, 4), "lens": l} for s, l in scored[: req.top_k]]}

    @app.post("/api/eval/run")
    def api_run_eval() -> dict[str, Any]:
        from eval.run_eval import run_eval

        job = jobs.start("golden_eval", "Golden Eval 全量回归评测", lambda: run_eval(base_url="http://127.0.0.1:18765"))
        return {"ok": True, "job": job}

    # =====================================================================
    # 6. Policy & Config Center
    # =====================================================================

    @app.get("/api/config/{name}")
    def api_get_config(name: str) -> dict[str, Any]:
        path = CONFIG_FILES.get(name)
        if path is None or not path.exists():
            raise HTTPException(status_code=404, detail=f"Unknown config '{name}'")
        return {"name": name, "path": str(path), "yaml_text": path.read_text(encoding="utf-8")}

    @app.put("/api/config/{name}")
    def api_put_config(name: str, req: ConfigUpdateRequest) -> dict[str, Any]:
        path = CONFIG_FILES.get(name)
        if path is None:
            raise HTTPException(status_code=404, detail=f"Unknown config '{name}'")
        try:
            parsed = yaml.safe_load(req.yaml_text)
            if not isinstance(parsed, dict):
                raise ValueError("Top-level YAML must be a mapping")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}") from exc
        with _CONFIG_LOCK:
            _atomic_write_text(path, req.yaml_text)
        lens_cache.clear()
        return {"ok": True, "name": name}

    return app


app = create_admin_app()


def run_admin_server(host: str = "127.0.0.1", port: int = 18770) -> None:
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_admin_server(
        host=os.getenv("GLINT_ADMIN_HOST", "127.0.0.1"),
        port=int(os.getenv("GLINT_ADMIN_PORT", "18770")),
    )
