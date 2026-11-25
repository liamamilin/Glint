"""M3 Evolution Flywheel: usage.db analytics, dynamic shelf scheduler, friction-to-draft, and SFT export.

Note: Prepares clean SFT training JSONL for future MLX LoRA fine-tuning without triggering training.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pipeline.l1_to_l2 import compile_l1_to_l2, load_existing_l2_lenses
from pipeline.ontology import LIB_ROOT
from pipeline.opencode_client import DEFAULT_MODEL, OpenCodeGoClient
from pipeline.schema import L1Material, content_sha256, make_stable_id

DEFAULT_USAGE_DB = LIB_ROOT.parent / "brain-server" / "data" / "usage.db"
TOKEN_RE = re.compile(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]+")


def _bigrams(text: str) -> set[str]:
    out: set[str] = set()
    for seg in TOKEN_RE.findall((text or "").lower()):
        if seg.isascii() or len(seg) == 1:
            out.add(seg)
        else:
            for i in range(len(seg) - 1):
                out.add(seg[i : i + 2])
    return out


def _read_usage_rows(db_path: Path, days: int = 30) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(usage_logs)").fetchall()}
        if not cols:
            return []
        select_cols = [
            c
            for c in [
                "id",
                "timestamp",
                "app_name",
                "skill_type",
                "input_text",
                "recommended_ids",
                "selected_id",
                "inject_mode",
                "latency_ms",
                "custom_intent",
                "predicted_primitive",
                "predicted_goal",
                "predicted_clarity",
                "release_version",
            ]
            if c in cols
        ]
        sql = f"SELECT {', '.join(select_cols)} FROM usage_logs WHERE timestamp >= ? ORDER BY id DESC"
        rows = [dict(r) for r in conn.execute(sql, (cutoff,)).fetchall()]
        for r in rows:
            try:
                r["recommended_ids"] = json.loads(r.get("recommended_ids") or "[]")
            except Exception:
                r["recommended_ids"] = []
        return rows
    finally:
        conn.close()


def compute_usage_analytics(
    db_path: Path = DEFAULT_USAGE_DB,
    l3_dir: Path = LIB_ROOT / "data" / "l3_production",
    days: int = 30,
) -> dict[str, Any]:
    """Compute M3 analytics dashboard metrics and per-card CTR from usage.db."""
    rows = _read_usage_rows(db_path, days=days)
    l3_map = load_existing_l2_lenses(l3_dir) if l3_dir.exists() else {}

    total = len(rows)
    clicks = 0
    escapes = 0
    customs = 0
    latencies: list[float] = []
    by_app: Counter[str] = Counter()
    by_day: Counter[str] = Counter()
    impressions: Counter[str] = Counter()
    card_clicks: Counter[str] = Counter()
    custom_items: list[dict[str, Any]] = []

    for r in rows:
        app = str(r.get("app_name") or "Unknown")
        by_app[app] += 1
        day = str(r.get("timestamp") or "")[:10]
        if day:
            by_day[day] += 1
        if r.get("latency_ms") is not None:
            latencies.append(float(r["latency_ms"]))

        for cid in r.get("recommended_ids") or []:
            impressions[str(cid)] += 1

        sel = r.get("selected_id")
        if sel is None:
            escapes += 1
        elif sel == "__custom__":
            customs += 1
            custom_items.append(
                {
                    "id": r.get("id"),
                    "timestamp": r.get("timestamp"),
                    "app_name": app,
                    "input_text": r.get("input_text", ""),
                    "custom_intent": r.get("custom_intent", ""),
                }
            )
        else:
            clicks += 1
            card_clicks[str(sel)] += 1

    sorted_lat = sorted(latencies)
    p50 = round(statistics.median(sorted_lat), 1) if sorted_lat else 0.0
    p95 = round(sorted_lat[min(len(sorted_lat) - 1, int(len(sorted_lat) * 0.95))], 1) if sorted_lat else 0.0

    top_cards = []
    for cid, clk in card_clicks.most_common(15):
        imp = max(1, impressions.get(cid, clk))
        lens = l3_map.get(cid)
        top_cards.append(
            {
                "id": cid,
                "title": lens.title if lens else cid,
                "primitive": lens.routing.primitive if lens else "-",
                "tier": lens.meta.tier if lens else "-",
                "shelf": lens.meta.shelf if lens else "-",
                "clicks": clk,
                "impressions": imp,
                "ctr": round(clk / imp, 3),
            }
        )

    return {
        "days": days,
        "total_events": total,
        "clicks": clicks,
        "escapes": escapes,
        "custom_intents": customs,
        "ctr": round(clicks / total, 3) if total else 0.0,
        "esc_rate": round(escapes / total, 3) if total else 0.0,
        "p50_ms": p50,
        "p95_ms": p95,
        "by_app": dict(by_app.most_common(12)),
        "by_day": dict(sorted(by_day.items())),
        "top_cards": top_cards,
        "custom_intent_logs": custom_items[:30],
        "recent_logs": [
            {
                "id": r.get("id"),
                "timestamp": r.get("timestamp"),
                "app_name": r.get("app_name"),
                "input_text": r.get("input_text"),
                "selected_id": r.get("selected_id"),
                "custom_intent": r.get("custom_intent"),
                "predicted_primitive": r.get("predicted_primitive"),
                "latency_ms": r.get("latency_ms"),
            }
            for r in rows[:30]
        ],
    }


def compute_shelf_proposals(
    db_path: Path = DEFAULT_USAGE_DB,
    l3_dir: Path = LIB_ROOT / "data" / "l3_production",
    overlay_path: Path | None = None,
    days: int = 30,
    min_impressions_demote: int = 5,
) -> dict[str, Any]:
    """Dynamic Vertical Shelf Scheduler: propose demoting stale/low-CTR domain cards & promoting L3 matches.

    Strictly exempts Tier S / evergreen lenses from auto-demotion.
    """
    rows = _read_usage_rows(db_path, days=days)
    l3_map = load_existing_l2_lenses(l3_dir) if l3_dir.exists() else {}
    if overlay_path is not None and overlay_path.exists():
        from pipeline.builder import load_overlay

        ov_file = load_overlay(overlay_path)
        for lid, ov in ov_file.overrides.items():
            lens = l3_map.get(lid)
            if lens is None:
                continue
            if ov.tier is not None:
                lens.meta.tier = ov.tier
            if ov.shelf is not None:
                lens.meta.shelf = ov.shelf

    impressions: Counter[str] = Counter()
    clicks: Counter[str] = Counter()
    friction_texts: list[str] = []

    for r in rows:
        for cid in r.get("recommended_ids") or []:
            impressions[str(cid)] += 1
        sel = r.get("selected_id")
        if sel is None:
            friction_texts.append(str(r.get("input_text") or ""))
        elif sel == "__custom__":
            friction_texts.append(f"{r.get('input_text', '')} {r.get('custom_intent', '')}")
        else:
            clicks[str(sel)] += 1

    # 1. Demote proposals: on_shelf domain_specific cards with high impressions and 0 clicks (or CTR < 5%)
    demote_proposals: list[dict[str, Any]] = []
    for lens in l3_map.values():
        if lens.meta.tier == "S" or lens.meta.shelf == "evergreen":
            continue
        if lens.meta.shelf != "on_shelf":
            continue
        imp = impressions.get(lens.id, 0)
        clk = clicks.get(lens.id, 0)
        if imp >= min_impressions_demote and (clk == 0 or clk / imp < 0.05):
            demote_proposals.append(
                {
                    "id": lens.id,
                    "title": lens.title,
                    "category": lens.meta.category,
                    "tier": lens.meta.tier,
                    "impressions": imp,
                    "clicks": clk,
                    "reason": f"近 {days} 天曝光 {imp} 次、点击 {clk} 次，建议下架回 L3 库存",
                }
            )
    demote_proposals.sort(key=lambda x: -x["impressions"])

    # 2. Promote proposals:
    # (a) HIGHEST PRIORITY: off_shelf L3 cards that broke through and were actually clicked by user!
    promote_proposals: list[dict[str, Any]] = []
    promoted_ids: set[str] = set()
    for lens in l3_map.values():
        if lens.meta.shelf != "off_shelf":
            continue
        clk = clicks.get(lens.id, 0)
        if clk >= 1:
            promoted_ids.add(lens.id)
            promote_proposals.append(
                {
                    "id": lens.id,
                    "title": lens.title,
                    "category": lens.meta.category,
                    "tier": lens.meta.tier,
                    "matched_count": clk * 10,
                    "score": 1.0,
                    "sample_friction": f"L3 穿透被选用 {clk} 次",
                    "reason": f"L3 断层穿透并被你实际选用 {clk} 次，强烈建议上架 L4",
                }
            )

    # (b) Match recent Esc / custom_intent friction against remaining off_shelf L3 inventory
    if friction_texts:
        friction_tokens = [(_bigrams(t), t) for t in friction_texts if t.strip()]
        for lens in l3_map.values():
            if lens.meta.shelf != "off_shelf" or lens.id in promoted_ids:
                continue
            lens_toks = _bigrams(
                f"{lens.title} {lens.tagline} {lens.routing.pain_point} {' '.join(lens.routing.trigger_utterances)}"
            )
            if not lens_toks:
                continue
            matched_samples: list[str] = []
            best_overlap = 0.0
            for f_toks, f_raw in friction_tokens:
                if not f_toks:
                    continue
                ov = len(f_toks & lens_toks) / max(3.0, float(len(f_toks)))
                if ov >= 0.25:
                    matched_samples.append(f_raw[:60])
                    best_overlap = max(best_overlap, ov)
            if matched_samples:
                promote_proposals.append(
                    {
                        "id": lens.id,
                        "title": lens.title,
                        "category": lens.meta.category,
                        "tier": lens.meta.tier,
                        "matched_count": len(matched_samples),
                        "score": round(best_overlap, 3),
                        "sample_friction": matched_samples[0],
                        "reason": f"命中 {len(matched_samples)} 条近期未满足意图，建议上架 L4",
                    }
                )
        promote_proposals.sort(key=lambda x: (-x["matched_count"], -x["score"]))

    evergreen_count = sum(1 for l in l3_map.values() if l.meta.tier == "S" or l.meta.shelf == "evergreen")
    return {
        "days": days,
        "evergreen_protected": evergreen_count,
        "demote_to_l3": demote_proposals[:20],
        "promote_to_l4": promote_proposals[:20],
    }


def export_sft_dataset(
    db_path: Path = DEFAULT_USAGE_DB,
    days: int = 180,
) -> list[dict[str, Any]]:
    """Export positive & custom_intent logs as SFT JSONL records (ready for future MLX LoRA)."""
    rows = _read_usage_rows(db_path, days=days)
    records: list[dict[str, Any]] = []
    for r in rows:
        sel = r.get("selected_id")
        if not sel:
            continue
        recs = [str(x) for x in (r.get("recommended_ids") or [])]
        negatives = [x for x in recs if x != sel]
        records.append(
            {
                "app_name": r.get("app_name"),
                "input_text": r.get("input_text"),
                "positive_id": sel,
                "negative_ids": negatives,
                "custom_intent": r.get("custom_intent"),
                "predicted_primitive": r.get("predicted_primitive"),
                "predicted_goal": r.get("predicted_goal"),
                "predicted_clarity": r.get("predicted_clarity"),
                "timestamp": r.get("timestamp"),
            }
        )
    return records


def synthesize_draft_from_friction(
    *,
    custom_intent: str,
    sample_input: str = "",
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """Convert a user's custom_intent friction into an L2 draft via OpenCode Go."""
    mat = L1Material(
        id=make_stable_id("usr", custom_intent[:40]),
        source="user-friction",
        source_ref="usage.db#custom_intent",
        title=custom_intent[:16].strip(),
        content=custom_intent.strip(),
        content_hash=content_sha256(custom_intent.strip()),
        suggested_action_type="inject_prompt",
        problem_solved=sample_input[:200] if sample_input else custom_intent[:200],
    )
    llm = OpenCodeGoClient(model=model, cwd=LIB_ROOT)
    summary = compile_l1_to_l2(
        [mat],
        llm=llm,
        l2_dir=LIB_ROOT / "data" / "l2_lenses",
        quarantine_dir=LIB_ROOT / "data" / "quarantine",
        embedder=None,
        workers=1,
        force=True,
    )
    return {"material_id": mat.id, "summary": summary.__dict__}
