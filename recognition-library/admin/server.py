"""Unified Glint Management Console API (L3/L4 Shelf, M3 Evolution Flywheel, M4 Forge, Eval & Policy)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
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
    parse_card_import_text,
    search_atlas_l0,
)
from pipeline.l1_to_l2 import load_existing_l2_lenses
from pipeline.l2_to_l3 import (
    DEFAULT_CLEANING_RULES_PATH,
    DEFAULT_SHELF_POLICY_PATH,
    DEFAULT_TAXONOMY_PATH,
    _merge_vault_shelf_policy,
    load_shelf_policy,
    load_taxonomy,
)
from pipeline.ontology import (
    DEFAULT_POLICY_PATH,
    LIB_ROOT,
    Ontology,
    load_policy,
    load_sources,
    resolve_meta_vault_dir,
)
from pipeline.schema import LensOverride, make_stable_id

ADMIN_DIR = Path(__file__).resolve().parent
L2_DIR = LIB_ROOT / "data" / "l2_lenses"
L3_DIR = LIB_ROOT / "data" / "l3_production"
OVERLAY_PATH = LIB_ROOT / "data" / "overlays" / "overrides.yaml"
QUARANTINE_DIR = LIB_ROOT / "data" / "quarantine"
DIST_DIR = LIB_ROOT / "dist"
META_VAULT_DIR: Path | None = None


def _get_vault_dir() -> Path | None:
    if META_VAULT_DIR is not None:
        return META_VAULT_DIR
    if L2_DIR == (LIB_ROOT / "data" / "l2_lenses"):
        return resolve_meta_vault_dir()
    return None

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
    is_vault: bool = False


class CollectionToggleRequest(BaseModel):
    active: bool


class CollectionRenameRequest(BaseModel):
    name: str = Field(..., min_length=1)


class LensCollectionUpdateRequest(BaseModel):
    add: str | None = None
    remove: str | None = None


class BatchOverlayRequest(BaseModel):
    lens_ids: list[str] = Field(..., min_length=1)
    shelf: str | None = None
    tier: str | None = None
    category: str | None = None
    add_collection: str | None = None


class CardImportParseRequest(BaseModel):
    raw_text: str = Field(..., min_length=1)


class ManualLensRequest(BaseModel):
    title: str = Field(..., min_length=2)
    tagline: str = ""
    primitive: str = "deconstruct"
    pain_point: str = Field(..., min_length=4)
    trigger_utterances: list[str] = Field(..., min_length=1)
    anti_triggers: list[str] = Field(default_factory=list)
    content: str = Field(..., min_length=4)
    action_type: str = "inject_prompt"
    inject_mode: str = "append"
    category: str = "custom"
    tier: str = "A"
    supersedes_id: str | None = None
    evolution_note: str | None = None
    target_vault: bool = False
    add_collection: str | None = None


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
    vault_dir = _get_vault_dir()
    vault_l2_dir = (vault_dir / "l2_lenses") if vault_dir is not None else None
    vault_l3_dir = (vault_dir / "l3_production") if vault_dir is not None else None
    vault_ov_path = (vault_dir / "overrides.yaml") if vault_dir is not None else None
    vault_sp_path = (vault_dir / "shelf_policy.yaml") if vault_dir is not None else None

    report_path = L3_DIR / "cleaning_report.json"
    r_mtime = report_path.stat().st_mtime_ns if report_path.exists() else -1
    o_mtime = OVERLAY_PATH.stat().st_mtime_ns if OVERLAY_PATH.exists() else -1
    s_mtime = DEFAULT_SHELF_POLICY_PATH.stat().st_mtime_ns if DEFAULT_SHELF_POLICY_PATH.exists() else -1
    l2_mtime = L2_DIR.stat().st_mtime_ns if L2_DIR.exists() else -1
    l3_mtime = L3_DIR.stat().st_mtime_ns if L3_DIR.exists() else -1
    vl2_mtime = vault_l2_dir.stat().st_mtime_ns if (vault_l2_dir is not None and vault_l2_dir.exists()) else -1
    vl3_mtime = vault_l3_dir.stat().st_mtime_ns if (vault_l3_dir is not None and vault_l3_dir.exists()) else -1
    vo_mtime = vault_ov_path.stat().st_mtime_ns if (vault_ov_path is not None and vault_ov_path.exists()) else -1
    vsp_mtime = vault_sp_path.stat().st_mtime_ns if (vault_sp_path is not None and vault_sp_path.exists()) else -1
    cache_key = (r_mtime, o_mtime, s_mtime, l2_mtime, l3_mtime, vl2_mtime, vl3_mtime, vo_mtime, vsp_mtime)
    if cache.get("key") == cache_key and cache.get("merged") is not None:
        return cache["merged"], cache["overlay"]

    ont = Ontology.load()
    overlay = load_overlay(OVERLAY_PATH, meta_vault_dir=vault_dir)
    sp = _merge_vault_shelf_policy(load_shelf_policy(DEFAULT_SHELF_POLICY_PATH), vault_dir)
    l3_map = load_existing_l2_lenses(L3_DIR) if L3_DIR.exists() else {}
    l2_map = load_existing_l2_lenses(L2_DIR)
    combined = dict(l2_map)
    combined.update(l3_map)
    vault_ids: set[str] = set()
    if vault_l2_dir is not None and vault_l2_dir.exists():
        vl2_map = load_existing_l2_lenses(vault_l2_dir)
        vault_ids.update(vl2_map.keys())
        combined.update(vl2_map)
    if vault_l3_dir is not None and vault_l3_dir.exists():
        vl3_map = load_existing_l2_lenses(vault_l3_dir)
        vault_ids.update(vl3_map.keys())
        combined.update(vl3_map)
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

    mtimes: dict[str, float] = {}
    for base_dir in [L2_DIR, vault_l2_dir]:
        if base_dir is not None and base_dir.exists():
            for p in base_dir.rglob("*.yaml"):
                try:
                    mtimes[p.stem] = p.stat().st_mtime
                except OSError:
                    continue
    cache["key"] = cache_key
    cache["merged"] = merged
    cache["overlay"] = overlay
    cache["vault_ids"] = vault_ids
    cache["mtimes"] = mtimes
    return merged, overlay


def _resolve_created_ts(lens: Any, mtimes: dict[str, float]) -> float:
    raw = getattr(lens.meta, "created_at", None)
    if raw:
        try:
            return datetime.fromisoformat(str(raw)).timestamp()
        except ValueError:
            pass
    return float(mtimes.get(lens.id, 0.0))


class _JobStore:
    """In-memory background job tracker for async M3/M4 LLM pipelines."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def start(self, kind: str, label: str, fn: Any, *, initial_stage: str = "") -> dict[str, Any]:
        job_id = f"{kind}_{uuid.uuid4().hex[:8]}"
        rec = {
            "id": job_id,
            "kind": kind,
            "label": label,
            "status": "running",
            "stage": initial_stage,
            "started_at": time.strftime("%H:%M:%S"),
            "finished_at": None,
            "result": None,
            "error": None,
        }
        with self._lock:
            self._jobs[job_id] = rec

        def _set_stage(text: str) -> None:
            with self._lock:
                if job_id in self._jobs:
                    self._jobs[job_id]["stage"] = text

        def _runner() -> None:
            try:
                try:
                    res = fn(_set_stage)
                except TypeError:
                    res = fn()
                with self._lock:
                    self._jobs[job_id]["status"] = "done"
                    self._jobs[job_id]["stage"] = ""
                    self._jobs[job_id]["result"] = res
                    self._jobs[job_id]["finished_at"] = time.strftime("%H:%M:%S")
            except Exception as exc:
                with self._lock:
                    self._jobs[job_id]["status"] = "failed"
                    self._jobs[job_id]["stage"] = ""
                    self._jobs[job_id]["error"] = str(exc)
                    self._jobs[job_id]["finished_at"] = time.strftime("%H:%M:%S")

        threading.Thread(target=_runner, daemon=True).start()
        return rec

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(list(self._jobs.values())))[:30]


def _is_personal_source(source: str) -> bool:
    s = (source or "").strip().lower()
    return s in {"manual-custom", "user-friction", "user-l0-forge", "user-forge"} or s.startswith(("forge-", "user-"))


def _load_all_l2_sources() -> tuple[dict[str, Any], set[str], Path | None]:
    l2_map = load_existing_l2_lenses(L2_DIR)
    vault_dir = _get_vault_dir()
    vault_ids: set[str] = set()
    if vault_dir is not None:
        vault_l2 = vault_dir / "l2_lenses"
        if vault_l2.exists():
            vl2_map = load_existing_l2_lenses(vault_l2)
            vault_ids = set(vl2_map.keys())
            l2_map.update(vl2_map)
    return l2_map, vault_ids, vault_dir


def _overlay_path_for(lens_id: str, vault_ids: set[str], vault_dir: Path | None) -> Path:
    if vault_dir is not None and lens_id in vault_ids:
        return vault_dir / "overrides.yaml"
    return OVERLAY_PATH


def _latest_source_mtime_ns() -> int:
    latest = 0
    paths_to_check = [OVERLAY_PATH, DEFAULT_SHELF_POLICY_PATH, DEFAULT_POLICY_PATH]
    dirs_to_scan = [L2_DIR]
    vault_dir = _get_vault_dir()
    if vault_dir is not None:
        paths_to_check.extend([vault_dir / "overrides.yaml", vault_dir / "shelf_policy.yaml"])
        dirs_to_scan.append(vault_dir / "l2_lenses")

    for p in paths_to_check:
        if p.exists():
            latest = max(latest, p.stat().st_mtime_ns)
    for d in dirs_to_scan:
        if d.exists():
            latest = max(latest, d.stat().st_mtime_ns)
            for p in d.rglob("*"):
                if p.is_dir() or p.suffix == ".yaml":
                    try:
                        latest = max(latest, p.stat().st_mtime_ns)
                    except OSError:
                        continue
    return latest


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
        shelf_policy = _merge_vault_shelf_policy(load_shelf_policy(DEFAULT_SHELF_POLICY_PATH), _get_vault_dir())
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
        manifest_mtime_ns = 0
        if manifest_path.exists():
            current_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_mtime_ns = manifest_path.stat().st_mtime_ns

        personal_total = sum(
            1 for lens in merged if lens.meta.status != "deprecated" and _is_personal_source(lens.meta.source)
        )
        l4_total = by_shelf["evergreen"] + by_shelf["on_shelf"]
        live_l4_total = int(current_manifest.get("total_active_lenses", 0)) if current_manifest else 0
        needs_rebuild = (
            current_manifest is None
            or live_l4_total != l4_total
            or _latest_source_mtime_ns() > manifest_mtime_ns
        )

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
            "l4_total": l4_total,
            "live_l4_total": live_l4_total,
            "personal_total": personal_total,
            "needs_rebuild": needs_rebuild,
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
        sort: str = "",
    ) -> dict[str, Any]:
        merged, overlay = _load_display_lenses_cached(lens_cache)
        mtimes = lens_cache.get("mtimes") or {}
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
            if shelf == "archived":
                if lens.meta.status != "deprecated":
                    continue
            elif shelf == "l4_all":
                if lens.meta.status != "active" or lens.meta.shelf not in {"evergreen", "on_shelf"}:
                    continue
            elif shelf == "personal":
                if lens.meta.status == "deprecated" or not _is_personal_source(lens.meta.source):
                    continue
            elif shelf:
                if lens.meta.status == "deprecated" or lens.meta.shelf != shelf:
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
            row["is_personal"] = _is_personal_source(lens.meta.source)
            row["is_vault"] = lens.id in (lens_cache.get("vault_ids") or set())
            row["created_at_ts"] = _resolve_created_ts(lens, mtimes)
            items.append(row)

        if sort == "newest":
            items.sort(key=lambda r: (-float(r.get("created_at_ts") or 0.0), str(r.get("id") or "")))

        return {"total": len(items), "items": items}

    @app.put("/api/overlays/{lens_id}")
    def upsert_overlay(lens_id: str, payload: LensOverride) -> dict[str, Any]:
        ont = Ontology.load()
        l2_map, vault_ids, vault_dir = _load_all_l2_sources()
        if lens_id not in l2_map:
            raise HTTPException(status_code=404, detail=f"Lens '{lens_id}' not found")
        if payload.primitive is not None and ont.normalize_primitive(payload.primitive) is None:
            raise HTTPException(status_code=400, detail=f"Invalid primitive '{payload.primitive}'")

        ov_path = _overlay_path_for(lens_id, vault_ids, vault_dir)
        overlay = load_overlay(ov_path)
        raw_map = {k: v.model_dump(exclude_none=True) for k, v in overlay.overrides.items()}
        new_fields = payload.model_dump(exclude_none=True)
        if new_fields:
            merged_entry = dict(raw_map.get(lens_id, {}))
            merged_entry.update(new_fields)
            raw_map[lens_id] = merged_entry
        else:
            raw_map.pop(lens_id, None)
        _save_overlay_file(ov_path, raw_map)
        lens_cache.clear()
        return {"ok": True, "lens_id": lens_id, "override": raw_map.get(lens_id, {})}

    @app.delete("/api/overlays/{lens_id}")
    def delete_overlay(lens_id: str) -> dict[str, Any]:
        _, vault_ids, vault_dir = _load_all_l2_sources()
        ov_path = _overlay_path_for(lens_id, vault_ids, vault_dir)
        overlay = load_overlay(ov_path)
        raw_map = {k: v.model_dump(exclude_none=True) for k, v in overlay.overrides.items()}
        raw_map.pop(lens_id, None)
        _save_overlay_file(ov_path, raw_map)
        lens_cache.clear()
        return {"ok": True, "lens_id": lens_id}

    @app.post("/api/overlays/{lens_id}/add-trigger")
    def add_trigger_overlay(lens_id: str, req: AddTriggerRequest) -> dict[str, Any]:
        l2_map, vault_ids, vault_dir = _load_all_l2_sources()
        if lens_id not in l2_map:
            raise HTTPException(status_code=404, detail=f"Lens '{lens_id}' not found")
        ov_path = _overlay_path_for(lens_id, vault_ids, vault_dir)
        overlay = load_overlay(ov_path)
        raw_map = {k: v.model_dump(exclude_none=True) for k, v in overlay.overrides.items()}
        entry = dict(raw_map.get(lens_id, {}))
        existing = list(entry.get("add_trigger_utterances") or [])
        utt = req.utterance.strip()
        if utt and utt not in existing:
            existing.append(utt)
        entry["add_trigger_utterances"] = existing
        # If previously removed, un-remove it
        if entry.get("remove_trigger_utterances"):
            entry["remove_trigger_utterances"] = [t for t in entry["remove_trigger_utterances"] if t != utt]
            if not entry["remove_trigger_utterances"]:
                entry.pop("remove_trigger_utterances", None)
        raw_map[lens_id] = entry
        _save_overlay_file(ov_path, raw_map)
        lens_cache.clear()
        return {"ok": True, "lens_id": lens_id, "add_trigger_utterances": existing}

    @app.post("/api/overlays/{lens_id}/remove-trigger")
    def remove_trigger_overlay(lens_id: str, req: AddTriggerRequest) -> dict[str, Any]:
        l2_map, vault_ids, vault_dir = _load_all_l2_sources()
        if lens_id not in l2_map:
            raise HTTPException(status_code=404, detail=f"Lens '{lens_id}' not found")
        utt = req.utterance.strip()
        ov_path = _overlay_path_for(lens_id, vault_ids, vault_dir)
        overlay = load_overlay(ov_path)
        raw_map = {k: v.model_dump(exclude_none=True) for k, v in overlay.overrides.items()}
        entry = dict(raw_map.get(lens_id, {}))
        added = list(entry.get("add_trigger_utterances") or [])
        if utt in added:
            added = [t for t in added if t != utt]
            if added:
                entry["add_trigger_utterances"] = added
            else:
                entry.pop("add_trigger_utterances", None)
        base_trigs = list(l2_map[lens_id].routing.trigger_utterances)
        if utt in base_trigs:
            removed = list(entry.get("remove_trigger_utterances") or [])
            if utt not in removed:
                removed.append(utt)
            entry["remove_trigger_utterances"] = removed
        if entry:
            raw_map[lens_id] = entry
        else:
            raw_map.pop(lens_id, None)
        _save_overlay_file(ov_path, raw_map)
        lens_cache.clear()
        return {"ok": True, "lens_id": lens_id, "removed": utt}

    @app.delete("/api/lenses/{lens_id}")
    def delete_personal_lens(lens_id: str) -> dict[str, Any]:
        l2_map, vault_ids, vault_dir = _load_all_l2_sources()
        lens = l2_map.get(lens_id)
        if lens is None:
            raise HTTPException(status_code=404, detail=f"卡片 '{lens_id}' 不存在")
        if not _is_personal_source(lens.meta.source):
            raise HTTPException(status_code=400, detail="内置卡片不支持删除，请改为「放回备用库」")
        removed_files = 0
        dirs_to_purge = [L2_DIR, L3_DIR]
        if vault_dir is not None and lens_id in vault_ids:
            dirs_to_purge.extend([vault_dir / "l2_lenses", vault_dir / "l3_production"])
        for base_dir in dirs_to_purge:
            if base_dir.exists():
                for p in list(base_dir.rglob(f"{lens_id}.yaml")):
                    p.unlink(missing_ok=True)
                    removed_files += 1
        # Also remove from overrides if present
        ov_path = _overlay_path_for(lens_id, vault_ids, vault_dir)
        overlay = load_overlay(ov_path)
        raw_map = {k: v.model_dump(exclude_none=True) for k, v in overlay.overrides.items()}
        if lens_id in raw_map:
            raw_map.pop(lens_id, None)
            _save_overlay_file(ov_path, raw_map)
        lens_cache.clear()
        return {"ok": True, "lens_id": lens_id, "removed_files": removed_files}

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
        q_clean = req.query.strip()
        for existing in gaps:
            if existing.get("status") != "resolved" and str(existing.get("query") or "").strip() == q_clean:
                if req.note.strip() and not existing.get("note"):
                    existing["note"] = req.note.strip()
                    _save_gaps(gaps)
                return {"ok": True, "gap": existing, "deduplicated": True}
        gap = {
            "id": f"gap_{uuid.uuid4().hex[:8]}",
            "query": q_clean,
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
        _, vault_ids, vault_dir = _load_all_l2_sources()
        updates_by_path: dict[Path, list[str]] = {}
        for lid in req.lens_ids:
            ov_path = _overlay_path_for(lid, vault_ids, vault_dir)
            updates_by_path.setdefault(ov_path, []).append(lid)

        for ov_path, target_ids in updates_by_path.items():
            overlay = load_overlay(ov_path)
            raw_map = {k: v.model_dump(exclude_none=True) for k, v in overlay.overrides.items()}
            for lid in target_ids:
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
            _save_overlay_file(ov_path, raw_map)

        if req.add_collection:
            vault_sp_path = (vault_dir / "shelf_policy.yaml") if vault_dir is not None else None
            vault_sp = load_shelf_policy(vault_sp_path) if (vault_sp_path is not None and vault_sp_path.exists()) else {}
            if vault_sp_path is not None and req.add_collection in (vault_sp.get("collections") or {}):
                cols = vault_sp.setdefault("collections", {})
                existing_ids = list(cols[req.add_collection].get("lens_ids") or [])
                cols[req.add_collection]["lens_ids"] = sorted(set(existing_ids + req.lens_ids))
                _save_yaml(vault_sp_path, vault_sp)
            else:
                if any(lid in vault_ids for lid in req.lens_ids):
                    raise HTTPException(
                        status_code=400,
                        detail="私有库卡片不能加入公开专题包（防止泄漏到公开配置），请加入私库专题包",
                    )
                sp = load_shelf_policy(DEFAULT_SHELF_POLICY_PATH)
                cols = sp.setdefault("collections", {})
                if req.add_collection in cols:
                    existing_ids = list(cols[req.add_collection].get("lens_ids") or [])
                    cols[req.add_collection]["lens_ids"] = sorted(set(existing_ids + req.lens_ids))
                    _save_yaml(DEFAULT_SHELF_POLICY_PATH, sp)

        lens_cache.clear()
        return {"ok": True, "updated": len(req.lens_ids)}

    @app.put("/api/shelf/category-mode")
    def set_category_mode(req: CategoryModeRequest) -> dict[str, Any]:
        sp = load_shelf_policy(DEFAULT_SHELF_POLICY_PATH)
        sp.setdefault("category_modes", {})[req.category] = req.mode
        _save_yaml(DEFAULT_SHELF_POLICY_PATH, sp)
        lens_cache.clear()
        return {"ok": True, "category": req.category, "mode": req.mode}

    @app.post("/api/shelf/collections")
    def create_collection(req: CollectionCreateRequest) -> dict[str, Any]:
        _, vault_ids, vault_dir = _load_all_l2_sources()
        use_vault = req.is_vault or any(lid in vault_ids for lid in req.lens_ids)
        if use_vault:
            if vault_dir is None:
                raise HTTPException(status_code=400, detail="未配置本地私库目录，无法创建私库专题包")
            target_path = vault_dir / "shelf_policy.yaml"
            sp = load_shelf_policy(target_path) if target_path.exists() else {"version": "1.0"}
            cid = make_stable_id("vcol", req.name)
        else:
            target_path = DEFAULT_SHELF_POLICY_PATH
            sp = load_shelf_policy(DEFAULT_SHELF_POLICY_PATH)
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
        _save_yaml(target_path, sp)
        lens_cache.clear()
        return {"ok": True, "id": cid, "is_vault": use_vault}

    @app.put("/api/shelf/collections/{cid}/toggle")
    def toggle_collection(cid: str, req: CollectionToggleRequest) -> dict[str, Any]:
        vault_dir = _get_vault_dir()
        vault_sp_path = (vault_dir / "shelf_policy.yaml") if vault_dir is not None else None
        vault_sp = load_shelf_policy(vault_sp_path) if (vault_sp_path is not None and vault_sp_path.exists()) else {}
        if vault_sp_path is not None and cid in (vault_sp.get("collections") or {}):
            target_path = vault_sp_path
            sp = vault_sp
        else:
            target_path = DEFAULT_SHELF_POLICY_PATH
            sp = load_shelf_policy(DEFAULT_SHELF_POLICY_PATH)
            if cid not in (sp.get("collections") or {}):
                raise HTTPException(status_code=404, detail=f"Collection '{cid}' not found")
        active_cols = list(sp.get("active_collections") or [])
        if req.active and cid not in active_cols:
            active_cols.append(cid)
        elif not req.active and cid in active_cols:
            active_cols.remove(cid)
        sp["active_collections"] = active_cols
        _save_yaml(target_path, sp)
        lens_cache.clear()
        return {"ok": True, "id": cid, "active": req.active}

    @app.put("/api/shelf/collections/{cid}")
    def rename_collection(cid: str, req: CollectionRenameRequest) -> dict[str, Any]:
        new_name = req.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="专题包名称不能为空")
        vault_dir = _get_vault_dir()
        vault_sp_path = (vault_dir / "shelf_policy.yaml") if vault_dir is not None else None
        vault_sp = load_shelf_policy(vault_sp_path) if (vault_sp_path is not None and vault_sp_path.exists()) else {}
        if vault_sp_path is not None and cid in (vault_sp.get("collections") or {}):
            target_path = vault_sp_path
            sp = vault_sp
        else:
            target_path = DEFAULT_SHELF_POLICY_PATH
            sp = load_shelf_policy(DEFAULT_SHELF_POLICY_PATH)
            if cid not in (sp.get("collections") or {}):
                raise HTTPException(status_code=404, detail=f"Collection '{cid}' not found")
        sp["collections"][cid]["name"] = new_name
        _save_yaml(target_path, sp)
        lens_cache.clear()
        return {"ok": True, "id": cid, "name": new_name}

    @app.delete("/api/shelf/collections/{cid}")
    def delete_collection(cid: str) -> dict[str, Any]:
        vault_dir = _get_vault_dir()
        vault_sp_path = (vault_dir / "shelf_policy.yaml") if vault_dir is not None else None
        vault_sp = load_shelf_policy(vault_sp_path) if (vault_sp_path is not None and vault_sp_path.exists()) else {}
        if vault_sp_path is not None and cid in (vault_sp.get("collections") or {}):
            target_path = vault_sp_path
            sp = vault_sp
        else:
            target_path = DEFAULT_SHELF_POLICY_PATH
            sp = load_shelf_policy(DEFAULT_SHELF_POLICY_PATH)
        (sp.get("collections") or {}).pop(cid, None)
        sp["active_collections"] = [c for c in (sp.get("active_collections") or []) if c != cid]
        _save_yaml(target_path, sp)
        lens_cache.clear()
        return {"ok": True, "id": cid}

    @app.put("/api/lenses/{lens_id}/collections")
    def update_lens_collections(lens_id: str, req: LensCollectionUpdateRequest) -> dict[str, Any]:
        l2_map, vault_ids, vault_dir = _load_all_l2_sources()
        if lens_id not in l2_map:
            raise HTTPException(status_code=404, detail=f"卡片 '{lens_id}' 不存在")
        is_vault_lens = lens_id in vault_ids
        vault_sp_path = (vault_dir / "shelf_policy.yaml") if vault_dir is not None else None
        vault_sp = load_shelf_policy(vault_sp_path) if (vault_sp_path is not None and vault_sp_path.exists()) else {}
        pub_sp = load_shelf_policy(DEFAULT_SHELF_POLICY_PATH)
        vault_cols = vault_sp.get("collections") or {}
        pub_cols = pub_sp.get("collections") or {}

        add_cid = (req.add or "").strip() or None
        rm_cid = (req.remove or "").strip() or None

        if add_cid:
            if add_cid not in vault_cols and add_cid not in pub_cols:
                raise HTTPException(status_code=404, detail=f"专题包 '{add_cid}' 不存在")
            if is_vault_lens and add_cid not in vault_cols:
                raise HTTPException(status_code=400, detail="私有库卡片不能加入公开专题包（防止泄漏），请加入私库专题包")

        vault_dirty = False
        pub_dirty = False
        if add_cid:
            if add_cid in vault_cols:
                ids = list(vault_cols[add_cid].get("lens_ids") or [])
                vault_cols[add_cid]["lens_ids"] = sorted(set(ids + [lens_id]))
                vault_dirty = True
            else:
                ids = list(pub_cols[add_cid].get("lens_ids") or [])
                pub_cols[add_cid]["lens_ids"] = sorted(set(ids + [lens_id]))
                pub_dirty = True
        if rm_cid:
            if rm_cid in vault_cols:
                vault_cols[rm_cid]["lens_ids"] = [x for x in (vault_cols[rm_cid].get("lens_ids") or []) if x != lens_id]
                vault_dirty = True
            elif rm_cid in pub_cols:
                pub_cols[rm_cid]["lens_ids"] = [x for x in (pub_cols[rm_cid].get("lens_ids") or []) if x != lens_id]
                pub_dirty = True

        if vault_dirty and vault_sp_path is not None:
            _save_yaml(vault_sp_path, vault_sp)
        if pub_dirty:
            _save_yaml(DEFAULT_SHELF_POLICY_PATH, pub_sp)
        lens_cache.clear()
        return {"ok": True, "lens_id": lens_id, "added": add_cid, "removed": rm_cid}

    _FLUFF_RE = re.compile(r"(请你作为|作为一名|资深专家|高级专家|请全面|请深入|请详细|请系统地|通俗易懂|专业且|高质量|尽可能详细)")
    _RIGID_RE = re.compile(r"(字以内|严格短于|不超过\d+字|固定[0-9一二三四五]|必须输出\d+条|只给\d+条)")
    _BOUNDARY_RE = re.compile(r"(例外|边界|不适用|跳过|不要|禁止|反模式|如果.*直接|前提|条件)")

    def _lens_usage_30d(lens_id: str) -> dict[str, int]:
        if not DEFAULT_USAGE_DB.exists():
            return {"impressions": 0, "clicks": 0, "escapes": 0}
        try:
            conn = sqlite3.connect(str(DEFAULT_USAGE_DB))
            cur = conn.cursor()
            cur.execute(
                "SELECT recommended_ids, selected_id FROM usage_events WHERE timestamp >= datetime('now', '-30 days')"
            )
            imp = clk = esc = 0
            for rec_raw, sel in cur.fetchall():
                try:
                    recs = json.loads(rec_raw) if isinstance(rec_raw, str) else []
                except Exception:
                    recs = []
                if lens_id in recs:
                    imp += 1
                    if sel == lens_id:
                        clk += 1
                    elif not sel:
                        esc += 1
            conn.close()
            return {"impressions": imp, "clicks": clk, "escapes": esc}
        except Exception:
            return {"impressions": 0, "clicks": 0, "escapes": 0}

    def _compute_lens_neighbors(lens_id: str, merged_map: dict[str, Any], top_n: int = 5) -> list[dict[str, Any]]:
        emb_path = DIST_DIR / "current" / "embeddings.npz"
        if not emb_path.exists():
            return []
        try:
            npz = np.load(emb_path, allow_pickle=False)
            ids = [str(x) for x in npz["ids"].tolist()]
            if lens_id not in ids:
                return []
            idx = ids.index(lens_id)
            vecs = np.asarray(npz["vectors"], dtype=np.float32)
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            vecs = vecs / np.maximum(norms, 1e-9)
            sims = (vecs @ vecs[idx]).reshape(-1)
            order = np.argsort(-sims)
            out: list[dict[str, Any]] = []
            for j in order:
                cid = ids[int(j)]
                if cid == lens_id:
                    continue
                cand = merged_map.get(cid)
                if cand is None or cand.meta.status == "deprecated":
                    continue
                out.append({
                    "id": cid,
                    "title": cand.title,
                    "primitive": cand.routing.primitive,
                    "cosine": round(float(sims[int(j)]), 3),
                    "pain_point": cand.routing.pain_point,
                    "triggers": list(cand.routing.trigger_utterances[:4]),
                })
                if len(out) >= top_n:
                    break
            return out
        except Exception:
            return []

    @app.get("/api/lenses/{lens_id}/polish-prompt")
    def api_lens_polish_prompt(lens_id: str, mode: str = "audit") -> dict[str, Any]:
        merged, _ = _load_display_lenses_cached(lens_cache)
        merged_map = {l.id: l for l in merged}
        lens = merged_map.get(lens_id)
        if lens is None:
            raise HTTPException(status_code=404, detail=f"卡片 '{lens_id}' 不存在")

        norm_mode = mode if mode in {"audit", "sharpen", "calibrate"} else "audit"
        content = lens.execution.content or ""
        trigs = [t.strip() for t in (lens.routing.trigger_utterances or []) if t.strip()]
        antis = [t.strip() for t in (lens.routing.anti_triggers or []) if t.strip()]
        neighbors = _compute_lens_neighbors(lens_id, merged_map, top_n=5)
        usage = _lens_usage_30d(lens_id)

        hints: list[dict[str, str]] = []
        if not antis:
            hints.append({"code": "missing_anti", "level": "warn", "label": "缺防串台边界 (anti_triggers)"})
        if len(trigs) < 3:
            hints.append({"code": "low_triggers", "level": "warn", "label": f"触发口语偏少 ({len(trigs)} 条)"})
        elif not any(len(t) <= 8 for t in trigs):
            hints.append({"code": "no_short_trigger", "level": "info", "label": "缺 4–8 字超短口语"})
        if not _BOUNDARY_RE.search(content):
            hints.append({"code": "missing_boundary", "level": "info", "label": "正文未见例外/适用边界"})
        fluff_found = sorted(set(_FLUFF_RE.findall(content)))
        if fluff_found:
            hints.append({"code": "fluff", "level": "warn", "label": f"疑似套话词：{'/'.join(fluff_found[:3])}"})
        rigid_found = sorted(set(_RIGID_RE.findall(content)))
        if rigid_found:
            hints.append({"code": "rigid", "level": "info", "label": f"含硬数量约束：{'/'.join(rigid_found[:2])}"})
        high_sim = [n for n in neighbors if n["cosine"] >= 0.82]
        if high_sim:
            hints.append({
                "code": "high_sim",
                "level": "warn",
                "label": f"{len(high_sim)} 张高相似老卡（最高 {high_sim[0]['cosine']:.2f}）",
            })

        card_yaml = yaml.safe_dump(
            {
                "id": lens.id,
                "title": lens.title,
                "tagline": lens.tagline,
                "primitive": lens.routing.primitive,
                "category": lens.meta.category,
                "tier": lens.meta.tier,
                "inject_mode": lens.execution.inject_mode or "append",
                "pain_point": lens.routing.pain_point,
                "trigger_utterances": trigs,
                "anti_triggers": antis,
                "content": content,
            },
            allow_unicode=True,
            sort_keys=False,
            width=120,
        ).strip()

        neighbor_lines = [
            f"{i}. [{n['primitive']}] {n['title']} (id={n['id']}, 相似度={n['cosine']:.2f})\n   痛点：{n['pain_point']}\n   代表口语：{' / '.join(n['triggers'])}"
            for i, n in enumerate(neighbors, start=1)
        ]
        hint_lines = [f"- {h['label']}" for h in hints] or ["- 静态结构完整，无明显硬伤"]
        usage_line = (
            f"近 30 天曝光 {usage['impressions']} 次，选用 {usage['clicks']} 次，Esc 跳过 {usage['escapes']} 次"
            if usage["impressions"] > 0
            else "近 30 天暂无使用记录"
        )

        if norm_mode == "sharpen":
            task_header = (
                "请对下面这张认知卡片做「外科锐化」：高信噪比优先，删去空话套话或把复杂经验压僵的伪硬约束，"
                "补齐核心有效机制、适用条件与例外出口（若需补齐机制与边界，允许新版长于原版，不要为了强行变短删掉关键边界）。\n"
                "请按 3 步输出：1) 病灶诊断（太水/跑偏 vs 过死/缺例外）；2) 删改与补强点；3) 文末附带可直接回填 Glint 的标准 ```yaml 代码块。"
            )
        elif norm_mode == "calibrate":
            task_header = (
                "请对下面这张认知卡片做「路由画像防串台校准」（尽量不动指令正文 content，除非发现执行面和路由面严重错位）：\n"
                "1) 对照下方最相似的现有竞争老卡，指出最可能漏召回或串台的原因；\n"
                "2) 避免在 pain_point 中写“提示词/模板/卡片”等元话语污染向量；\n"
                "3) 给出高信噪比 trigger_utterances（含 1–2 条 4–8 字超短口语，不凑数）与明确的 anti_triggers（划清与邻卡的边界）；\n"
                "4) 文末附带可直接回填 Glint 的标准 ```yaml 代码块。"
            )
        else:
            task_header = (
                "请对下面这张认知卡片做「全面体检与打磨」，同时覆盖内容质量与路由区分度：\n"
                "1) 含水量与僵化度审计：检查是否有空洞套话，或把复杂经验压死的僵硬模板约束；\n"
                "2) 机制与边界检查：是否保留了方法有效的原因、适用条件与例外/跳过出口；\n"
                "3) 防串台校准：对照下方本机最相似的现有竞争老卡，检查 pain_point、trigger_utterances（含 1–2 条超短口语）与 anti_triggers 是否边界清晰；\n"
                "4) 若某部分本就扎实，请直接确认无需强行改动；最后附带可直接回填 Glint 的标准 ```yaml 代码块。"
            )

        wrap_note = "（注意：本卡为 wrap 模式，content 中必须保留 {{input}} 占位符）" if (lens.execution.inject_mode == "wrap") else ""
        prompt = (
            f"{task_header}\n\n"
            f"【目标卡片】\n```yaml\n{card_yaml}\n```\n\n"
            f"【本机客观参考（纯本地检测，供诊断参考）】\n"
            f"- 静态线索：\n" + "\n".join(hint_lines) + "\n"
            f"- 近期使用：{usage_line}\n"
            f"- 库中最相似的现有卡片（防串台对照）：\n"
            + ("\n".join(neighbor_lines) if neighbor_lines else "  （暂无向量索引，可先构建卡片库）")
            + "\n\n"
            f"【回填格式要求】{wrap_note}\n"
            "请在回答最后输出单个标准 ```yaml 代码块，包含扁平字段："
            "title、tagline、primitive、category、tier、inject_mode、pain_point、trigger_utterances、anti_triggers、evolution_note（一句话说明改动点）、content。"
        )
        return {
            "ok": True,
            "lens_id": lens_id,
            "mode": norm_mode,
            "hints": hints,
            "neighbors": neighbors,
            "usage": usage,
            "prompt": prompt,
        }

    @app.post("/api/lenses/parse-import")
    def api_parse_import(req: CardImportParseRequest) -> dict[str, Any]:
        merged, _ = _load_display_lenses_cached(lens_cache)
        existing_map = {l.id: l for l in merged}
        vault_ids = lens_cache.get("vault_ids") or set()
        return parse_card_import_text(req.raw_text, existing_lenses=existing_map, vault_ids=vault_ids)

    @app.post("/api/lenses/manual")
    def api_create_manual_lens(req: ManualLensRequest) -> dict[str, Any]:
        # Validate ALL invariants before any filesystem mutation (atomic pre-flight gate)
        l2_map, vault_ids, vault_dir = _load_all_l2_sources()
        if req.target_vault:
            if vault_dir is None:
                raise HTTPException(status_code=400, detail="未配置本地私库目录，无法存入私库")
            target_l2_dir = vault_dir / "l2_lenses"
        else:
            target_l2_dir = L2_DIR

        merged_display, _ = _load_display_lenses_cached(lens_cache)
        merged_map = {l.id: l for l in merged_display}

        supersedes_id = (req.supersedes_id or "").strip() or None
        next_version = 1
        if supersedes_id:
            if supersedes_id not in l2_map and supersedes_id not in merged_map:
                raise HTTPException(status_code=404, detail=f"未找到待归档的前代卡片 '{supersedes_id}'")
            pred = merged_map.get(supersedes_id) or l2_map[supersedes_id]
            if pred.meta.status == "deprecated":
                raise HTTPException(status_code=409, detail=f"前代卡片 '{supersedes_id}' 已归档，请勿重复归档覆盖演进链")
            if supersedes_id in vault_ids and not req.target_vault:
                raise HTTPException(
                    status_code=400,
                    detail="前代卡片属于本地私库，替代新版必须同样存入本地私库（防止私有卡片 ID 泄漏至公开仓库）",
                )
            next_version = int(pred.meta.version or 1) + 1

        col_id = (req.add_collection or "").strip() or None
        vault_sp_path = (vault_dir / "shelf_policy.yaml") if vault_dir is not None else None
        vault_sp = load_shelf_policy(vault_sp_path) if (vault_sp_path is not None and vault_sp_path.exists()) else {}
        col_is_vault = bool(col_id and vault_sp_path is not None and col_id in (vault_sp.get("collections") or {}))
        if col_id and req.target_vault and not col_is_vault:
            raise HTTPException(status_code=400, detail="私库卡片不能加入公开专题包（防止泄漏）")

        lens = create_manual_lens(
            title=req.title,
            tagline=req.tagline,
            primitive=req.primitive,
            pain_point=req.pain_point,
            trigger_utterances=req.trigger_utterances,
            anti_triggers=req.anti_triggers,
            content=req.content,
            action_type=req.action_type,
            inject_mode=req.inject_mode,
            category=req.category,
            tier=req.tier,
            supersedes=supersedes_id,
            evolution_note=req.evolution_note,
            version=next_version,
            l2_dir=target_l2_dir,
        )

        # Archive predecessor via Overlay (never mutates read-only upstream YAML; strictly respects meta-vault isolation)
        if supersedes_id:
            ov_path = _overlay_path_for(supersedes_id, vault_ids, vault_dir)
            overlay = load_overlay(ov_path)
            raw_map = {k: v.model_dump(exclude_none=True) for k, v in overlay.overrides.items()}
            entry = dict(raw_map.get(supersedes_id, {}))
            entry["status"] = "deprecated"
            entry["superseded_by"] = lens.id
            if req.evolution_note and req.evolution_note.strip():
                entry["evolution_note"] = req.evolution_note.strip()
            raw_map[supersedes_id] = entry
            _save_overlay_file(ov_path, raw_map)

        # Optionally attach new card to a collection (pre-validated above)
        if col_id:
            if col_is_vault and vault_sp_path is not None:
                cols = vault_sp.setdefault("collections", {})
                existing_ids = list(cols[col_id].get("lens_ids") or [])
                cols[col_id]["lens_ids"] = sorted(set(existing_ids + [lens.id]))
                _save_yaml(vault_sp_path, vault_sp)
            else:
                sp = load_shelf_policy(DEFAULT_SHELF_POLICY_PATH)
                cols = sp.setdefault("collections", {})
                if col_id in cols:
                    existing_ids = list(cols[col_id].get("lens_ids") or [])
                    cols[col_id]["lens_ids"] = sorted(set(existing_ids + [lens.id]))
                    _save_yaml(DEFAULT_SHELF_POLICY_PATH, sp)

        lens_cache.clear()
        return {"ok": True, "lens": lens.model_dump(), "archived_predecessor": supersedes_id}

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

    def _execute_build(*, blocking: bool = False) -> tuple[Path, Any]:
        if not _BUILD_LOCK.acquire(blocking=blocking):
            raise HTTPException(status_code=409, detail="已有清洗发布任务正在进行中，请稍候…")
        try:
            policy = load_policy()
            embed_model = policy.get("models", {}).get("embedding_model", DEFAULT_EMBED_MODEL)
            ollama_url = policy.get("models", {}).get("ollama_base_url", "http://127.0.0.1:11434")
            client = OllamaEmbedClient(model=embed_model, base_url=ollama_url)
            embedder = client if client.is_available() else None
            release_dir, manifest = build_release(
                l2_dir=L2_DIR,
                l3_dir=L3_DIR,
                overlay_path=OVERLAY_PATH,
                quarantine_dir=QUARANTINE_DIR,
                dist_dir=DIST_DIR,
                embedder=embedder,
                meta_vault_dir=_get_vault_dir(),
            )
            lens_cache.clear()
            return release_dir, manifest
        finally:
            _BUILD_LOCK.release()

    @app.post("/api/build")
    def trigger_build() -> dict[str, Any]:
        try:
            release_dir, manifest = _execute_build(blocking=False)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "release_dir": str(release_dir), "manifest": manifest.model_dump()}

    @app.post("/api/rollback")
    def trigger_rollback(req: RollbackRequest) -> dict[str, Any]:
        try:
            chosen = rollback_release(DIST_DIR, target_version=req.version)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        lens_cache.clear()
        return {"ok": True, "active_version": chosen}

    # =====================================================================
    # 3. M3 Evolution Flywheel (Analytics, Shelf Proposals, Friction, SFT Export)
    # =====================================================================

    @app.get("/api/evolution/analytics")
    def api_evolution_analytics(days: int = 30) -> dict[str, Any]:
        return compute_usage_analytics(
            db_path=DEFAULT_USAGE_DB,
            l3_dir=L3_DIR,
            days=days,
            meta_vault_dir=_get_vault_dir(),
        )

    @app.get("/api/evolution/proposals")
    def api_evolution_proposals(days: int = 30, min_imp: int = 5) -> dict[str, Any]:
        return compute_shelf_proposals(
            db_path=DEFAULT_USAGE_DB,
            l3_dir=L3_DIR,
            overlay_path=OVERLAY_PATH,
            days=days,
            min_impressions_demote=min_imp,
            meta_vault_dir=_get_vault_dir(),
        )

    @app.post("/api/evolution/friction-to-draft")
    def api_friction_to_draft(req: FrictionDraftRequest) -> dict[str, Any]:
        def _run(set_stage: Any) -> dict[str, Any]:
            set_stage("① AI 正在起草新卡片内容与触发口语（约 20–40 秒）…")
            res = synthesize_draft_from_friction(
                custom_intent=req.custom_intent,
                sample_input=req.sample_input,
            )
            lens_cache.clear()
            compiled = int((res.get("summary") or {}).get("compiled", 0))
            if compiled > 0:
                set_stage("② 卡片已生成，正在自动同步索引使其立即可匹配（约 10–15 秒）…")
                try:
                    _, manifest = _execute_build(blocking=True)
                    res["auto_synced"] = True
                    res["total_active_lenses"] = manifest.total_active_lenses
                except Exception as exc:
                    res["auto_synced"] = False
                    res["sync_error"] = str(exc)
                # Auto-resolve matching open gaps so the gap list stays clean
                q_match = req.custom_intent.strip()
                gaps = _load_gaps()
                changed = False
                for g in gaps:
                    if g.get("status") != "resolved" and str(g.get("query") or "").strip() == q_match:
                        g["status"] = "resolved"
                        g["note"] = f"已自动生成卡片（{res.get('material_id', '')}）"
                        changed = True
                if changed:
                    _save_gaps(gaps)
            return res

        job = jobs.start(
            "friction_draft",
            f"AI 生成新卡片：{req.custom_intent[:24]}",
            _run,
            initial_stage="① AI 正在起草新卡片内容与触发口语（约 20–40 秒）…",
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
        def _run(set_stage: Any) -> dict[str, Any]:
            set_stage("① AI 正在从正文提炼思考卡片（约 20–45 秒）…")
            res = ingest_l0_raw_text(title=req.title, raw_text=req.raw_text)
            lens_cache.clear()
            compiled = int((res.get("l2_summary") or res.get("summary") or {}).get("compiled", 0))
            if res.get("stage") == "compiled_to_l2" and compiled > 0:
                if res.get("item_id") and not res.get("material_id"):
                    res["material_id"] = res["item_id"]
                set_stage("② 提炼完成，正在自动同步索引使其立即可匹配（约 10–15 秒）…")
                try:
                    _, manifest = _execute_build(blocking=True)
                    res["auto_synced"] = True
                    res["total_active_lenses"] = manifest.total_active_lenses
                except Exception as exc:
                    res["auto_synced"] = False
                    res["sync_error"] = str(exc)
            return res

        job = jobs.start(
            "l0_ingest",
            f"文章提炼卡片：{req.title[:24]}",
            _run,
            initial_stage="① AI 正在从正文提炼思考卡片（约 20–45 秒）…",
        )
        return {"ok": True, "job": job}

    @app.post("/api/forge/starter-pack")
    def api_forge_starter_pack(req: ForgePackRequest) -> dict[str, Any]:
        def _run(set_stage: Any) -> dict[str, Any]:
            set_stage("① AI 正在定制 7 张全视角入门卡片（约 1–2 分钟）…")
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
                set_stage("② 7 张卡包已生成，正在自动同步索引使其立即可匹配（约 10–15 秒）…")
                try:
                    _, manifest = _execute_build(blocking=True)
                    res["auto_synced"] = True
                    res["total_active_lenses"] = manifest.total_active_lenses
                except Exception as exc:
                    res["auto_synced"] = False
                    res["sync_error"] = str(exc)
            return res

        job = jobs.start(
            "forge_pack",
            f"生成 7 张专题卡包：{req.domain_name[:20]}",
            _run,
            initial_stage="① AI 正在定制 7 张全视角入门卡片（约 1–2 分钟）…",
        )
        return {"ok": True, "job": job}

    @app.get("/api/forge/atlas-search")
    def api_atlas_search(q: str = "", limit: int = 25) -> dict[str, Any]:
        return {"items": search_atlas_l0(query=q, limit=min(limit, 50))}

    @app.post("/api/forge/atlas-import")
    def api_atlas_import(req: AtlasImportRequest) -> dict[str, Any]:
        def _run(set_stage: Any) -> dict[str, Any]:
            set_stage(f"① 正在精炼 {len(req.offsets)} 条 ATLAS 素材…")
            res = import_atlas_offsets(req.offsets)
            lens_cache.clear()
            compiled = int((res.get("l2_summary") or {}).get("compiled", 0))
            if compiled > 0:
                set_stage("② 精炼完成，正在自动同步卡片库索引…")
                try:
                    _, manifest = _execute_build(blocking=True)
                    res["auto_synced"] = True
                    res["total_active_lenses"] = manifest.total_active_lenses
                except Exception as exc:
                    res["auto_synced"] = False
                    res["sync_error"] = str(exc)
            return res

        job = jobs.start(
            "atlas_import",
            f"精选素材入库（{len(req.offsets)} 条）",
            _run,
            initial_stage=f"① 正在精炼 {len(req.offsets)} 条 ATLAS 素材…",
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
