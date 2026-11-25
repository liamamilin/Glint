"""Probabilistic Spaced-Discovery engine & persistent state for Glint Seed (Ambient HUD)."""

from __future__ import annotations

import json
import os
import random
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# Base tier weights inside L4 (higher = surfaced more frequently before behavior/recall modulation)
TIER_BASE_WEIGHTS: dict[str, float] = {
    "S": 4.0,
    "A": 2.0,
    "B": 1.0,
    "C": 0.6,
}

# Target exploration probability share for L3 dormant cards in `smart_mix` mode
L3_SMART_MIX_SHARE: float = 0.15

# Anti-repeat cooldown window (last N drawn cards get heavily damped)
RECENT_SHOWN_WINDOW: int = 25


class SeedStore:
    """Thread-safe, atomic JSON store + weighted sampler for Glint Seed.

    Tracks:
    - muted_ids: permanently excluded from passive Seed carousel (active hotkey recall untouched)
    - snoozed_until: temporarily hidden until epoch timestamp
    - skip_counts: manual quick-skip counts (gently lowers probability)
    - snooze_counts: historical snooze counts (returns softer after snooze expires)
    - category_interest: positive signals from expand/copy actions (capped to prevent filter bubbles)
    - recent_shown: rolling list of recently drawn IDs for spaced freshness
    """

    def __init__(self, path: Path, rng: random.Random | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._rng = rng or random.Random()

    def _default_config(self) -> dict[str, float]:
        return {
            "tier_s_weight": TIER_BASE_WEIGHTS["S"],
            "tier_a_weight": TIER_BASE_WEIGHTS["A"],
            "tier_b_weight": TIER_BASE_WEIGHTS["B"],
            "l3_smart_mix_share": L3_SMART_MIX_SHARE,
            "recall_decay_rate": 0.35,
            "skip_decay_factor": 0.72,
        }

    def _default_state(self) -> dict[str, Any]:
        return {
            "config": self._default_config(),
            "muted_ids": [],
            "snoozed_until": {},
            "skip_counts": {},
            "snooze_counts": {},
            "category_interest": {},
            "recent_shown": [],
            "updated_at": 0.0,
        }

    def _load_unlocked(self) -> dict[str, Any]:
        state = self._default_state()
        if not self._path.exists():
            return state
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return state
        if not isinstance(raw, dict):
            return state

        now = time.time()
        muted = sorted({str(x).strip() for x in (raw.get("muted_ids") or []) if str(x).strip()})
        muted_set = set(muted)

        snoozed: dict[str, float] = {}
        for k, v in (raw.get("snoozed_until") or {}).items():
            key = str(k).strip()
            try:
                ts = float(v)
                if key and key not in muted_set and ts > now:
                    snoozed[key] = ts
            except (TypeError, ValueError):
                continue

        def _int_map(name: str) -> dict[str, int]:
            out: dict[str, int] = {}
            for k, v in (raw.get(name) or {}).items():
                key = str(k).strip()
                try:
                    iv = int(v)
                    if key and iv > 0:
                        out[key] = iv
                except (TypeError, ValueError):
                    continue
            return out

        recent = [str(x).strip() for x in (raw.get("recent_shown") or []) if str(x).strip()][-RECENT_SHOWN_WINDOW:]

        cfg = self._default_config()
        raw_cfg = raw.get("config")
        if isinstance(raw_cfg, dict):
            for k in list(cfg.keys()):
                if k in raw_cfg:
                    try:
                        cfg[k] = float(raw_cfg[k])
                    except (TypeError, ValueError):
                        pass

        state["config"] = cfg
        state["muted_ids"] = muted
        state["snoozed_until"] = snoozed
        state["skip_counts"] = _int_map("skip_counts")
        state["snooze_counts"] = _int_map("snooze_counts")
        state["category_interest"] = _int_map("category_interest")
        state["recent_shown"] = recent
        return state

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "config": state.get("config") or self._default_config(),
            "muted_ids": sorted(set(state.get("muted_ids") or [])),
            "snoozed_until": {k: round(float(v), 3) for k, v in sorted((state.get("snoozed_until") or {}).items())},
            "skip_counts": dict(sorted((state.get("skip_counts") or {}).items())),
            "snooze_counts": dict(sorted((state.get("snooze_counts") or {}).items())),
            "category_interest": dict(sorted((state.get("category_interest") or {}).items())),
            "recent_shown": list(state.get("recent_shown") or [])[-RECENT_SHOWN_WINDOW:],
            "updated_at": round(time.time(), 3),
        }
        fd, tmp_name = tempfile.mkstemp(prefix=".seed_state_", suffix=".tmp", dir=str(self._path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self._path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def get_exclusions(self) -> tuple[set[str], dict[str, float]]:
        with self._lock:
            st = self._load_unlocked()
            return set(st["muted_ids"]), dict(st["snoozed_until"])

    def apply_action(
        self,
        lens_id: str,
        action: str,
        snooze_days: int = 7,
        category: str | None = None,
    ) -> tuple[int, int]:
        clean_id = lens_id.strip()
        with self._lock:
            st = self._load_unlocked()
            muted = set(st["muted_ids"])
            snoozed = dict(st["snoozed_until"])
            skips = dict(st["skip_counts"])
            snooze_hist = dict(st["snooze_counts"])
            cat_int = dict(st["category_interest"])

            if action == "mute":
                muted.add(clean_id)
                snoozed.pop(clean_id, None)
            elif action == "snooze":
                days = max(1, min(int(snooze_days), 365))
                snoozed[clean_id] = time.time() + days * 86400.0
                muted.discard(clean_id)
                snooze_hist[clean_id] = snooze_hist.get(clean_id, 0) + 1
            elif action == "unmute":
                muted.discard(clean_id)
                snoozed.pop(clean_id, None)
            elif action == "skip":
                skips[clean_id] = min(20, skips.get(clean_id, 0) + 1)
            elif action in {"expand", "copy"}:
                # Positive interest signal: gentle category affinity boost + clear skip penalty on this card
                skips.pop(clean_id, None)
                if category and category.strip():
                    cat_key = category.strip().lower()
                    delta = 2 if action == "copy" else 1
                    cat_int[cat_key] = min(15, cat_int.get(cat_key, 0) + delta)

            st["muted_ids"] = sorted(muted)
            st["snoozed_until"] = snoozed
            st["skip_counts"] = skips
            st["snooze_counts"] = snooze_hist
            st["category_interest"] = cat_int
            self._save_unlocked(st)
            return len(muted), len(snoozed)

    def get_state_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._load_unlocked()

    def update_config(self, updates: dict[str, float]) -> dict[str, float]:
        with self._lock:
            st = self._load_unlocked()
            cfg = dict(st["config"])
            for k, v in updates.items():
                if k in cfg:
                    cfg[k] = round(float(v), 4)
            st["config"] = cfg
            self._save_unlocked(st)
            return cfg

    def reset_all(self) -> tuple[int, int]:
        with self._lock:
            st = self._load_unlocked()
            fresh = self._default_state()
            fresh["config"] = st["config"]
            self._save_unlocked(fresh)
            return 0, 0

    def score_candidates(
        self,
        l4_lenses: list[dict[str, Any]],
        l3_lenses: list[dict[str, Any]],
        recall_counts: dict[str, int],
        mode: str = "smart_mix",
        exclude_ids: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Compute live weighted seed candidates with normalized probabilities."""
        with self._lock:
            st = self._load_unlocked()

        muted = set(st["muted_ids"])
        snoozed = set(st["snoozed_until"].keys())
        skips: dict[str, int] = st["skip_counts"]
        snooze_hist: dict[str, int] = st["snooze_counts"]
        cat_int: dict[str, int] = st["category_interest"]
        recent_set = set(st["recent_shown"])
        extra_excl = exclude_ids or set()
        hard_excluded = muted | snoozed

        # 1. Select layer candidates by mode
        pool_pairs: list[tuple[dict[str, Any], str]] = []
        if mode == "tier_s":
            for lens in l4_lenses:
                if str((lens.get("meta") or {}).get("tier") or "").upper() == "S":
                    pool_pairs.append((lens, "L4"))
        elif mode == "l4_all":
            pool_pairs.extend((lens, "L4") for lens in l4_lenses)
        elif mode == "l3_discovery":
            pool_pairs.extend((lens, "L3") for lens in l3_lenses)
        else:  # smart_mix
            pool_pairs.extend((lens, "L4") for lens in l4_lenses)
            pool_pairs.extend((lens, "L3") for lens in l3_lenses)

        # Deduplicate by id (prefer L4 if ever overlapping) and filter hard exclusions
        seen_ids: set[str] = set()
        eligible: list[tuple[dict[str, Any], str]] = []
        for lens, layer in pool_pairs:
            lid = str(lens.get("id", "")).strip()
            if not lid or lid in seen_ids or lid in hard_excluded:
                continue
            seen_ids.add(lid)
            eligible.append((lens, layer))

        if not eligible:
            return [], len(muted), len(snoozed)

        # Strictly exclude caller-specified exclude_ids (e.g. current card on "next") if alternatives exist
        if extra_excl:
            filtered = [(lens, layer) for lens, layer in eligible if str(lens.get("id", "")) not in extra_excl]
            if filtered:
                eligible = filtered

        # Check whether there are non-recent items so cooldown doesn't starve tiny pools
        has_non_recent = any(str(lens.get("id", "")) not in recent_set for lens, _ in eligible)

        cfg: dict[str, float] = st["config"]
        tier_weights = {
            "S": float(cfg.get("tier_s_weight", TIER_BASE_WEIGHTS["S"])),
            "A": float(cfg.get("tier_a_weight", TIER_BASE_WEIGHTS["A"])),
            "B": float(cfg.get("tier_b_weight", TIER_BASE_WEIGHTS["B"])),
            "C": 0.6,
        }
        recall_decay_rate = float(cfg.get("recall_decay_rate", 0.35))
        skip_decay_factor = float(cfg.get("skip_decay_factor", 0.72))
        l3_share = min(0.8, max(0.0, float(cfg.get("l3_smart_mix_share", L3_SMART_MIX_SHARE))))

        scored: list[dict[str, Any]] = []
        l4_weight_sum = 0.0
        l3_weight_sum = 0.0

        for lens, layer in eligible:
            seed = lens_to_seed_dict(lens, layer=layer)
            lid = seed["id"]
            tier = seed["tier"].upper()
            cat_key = seed["category"].lower()

            base_w = tier_weights.get(tier, tier_weights["B"]) if layer == "L4" else 1.0
            recalls = recall_counts.get(lid, 0)
            # Anti-echo-chamber decay: frequent hotkey recalls -> lower passive carousel weight
            m_recall = max(0.25, 1.0 / (1.0 + recall_decay_rate * recalls))

            skip_n = skips.get(lid, 0)
            snooze_n = snooze_hist.get(lid, 0)
            m_behavior = max(0.15, skip_decay_factor**skip_n) * max(0.35, 0.75**snooze_n)

            # Gentle category interest boost (capped at 1.6x)
            m_affinity = min(1.6, 1.0 + 0.08 * cat_int.get(cat_key, 0))

            # Anti-repeat freshness damping
            if has_non_recent and lid in recent_set:
                m_fresh = 0.02
            else:
                m_fresh = 1.0

            w = max(0.001, base_w * m_recall * m_behavior * m_affinity * m_fresh)
            seed["weight"] = round(w, 4)
            seed["recall_count_30d"] = recalls
            seed["reason_tag"] = _build_reason_tag(layer, tier, recalls, skip_n)

            if layer == "L4":
                l4_weight_sum += w
            else:
                l3_weight_sum += w
            scored.append(seed)

        # 2. In smart_mix mode, normalize L3 total weight to configured l3_smart_mix_share when both layers exist
        if mode == "smart_mix" and l4_weight_sum > 0.0 and l3_weight_sum > 0.0:
            if l3_share <= 0.0:
                scale_l3 = 0.0
            else:
                target_l3_sum = l4_weight_sum * (l3_share / (1.0 - l3_share))
                scale_l3 = target_l3_sum / l3_weight_sum
            for seed in scored:
                if seed["layer"] == "L3":
                    seed["weight"] = round(seed["weight"] * scale_l3, 4)

        total_w = sum(s["weight"] for s in scored) or 1.0
        for seed in scored:
            seed["probability_pct"] = round(100.0 * seed["weight"] / total_w, 2)

        return scored, len(muted), len(snoozed)

    def draw_next(
        self,
        l4_lenses: list[dict[str, Any]],
        l3_lenses: list[dict[str, Any]],
        recall_counts: dict[str, int],
        mode: str = "smart_mix",
        exclude_ids: set[str] | None = None,
    ) -> tuple[dict[str, Any] | None, int, int, int]:
        """Sample one next seed card according to live dynamic probabilities and record impression."""
        scored, muted_count, snoozed_count = self.score_candidates(
            l4_lenses=l4_lenses,
            l3_lenses=l3_lenses,
            recall_counts=recall_counts,
            mode=mode,
            exclude_ids=exclude_ids,
        )
        if not scored:
            return None, 0, muted_count, snoozed_count

        weights = [max(1e-6, float(s["weight"])) for s in scored]
        picked = self._rng.choices(scored, weights=weights, k=1)[0]

        with self._lock:
            st = self._load_unlocked()
            recent = [x for x in st["recent_shown"] if x != picked["id"]]
            recent.append(picked["id"])
            st["recent_shown"] = recent[-RECENT_SHOWN_WINDOW:]
            self._save_unlocked(st)

        return picked, len(scored), muted_count, snoozed_count


def _build_reason_tag(layer: str, tier: str, recalls: int, skips: int) -> str:
    if layer == "L3":
        base = "L3 休眠探索 · 破盲"
    elif tier == "S":
        base = "Tier S 核心心法"
    elif tier == "A":
        base = "Tier A 高频工具"
    else:
        base = "L4 广度认知"

    if recalls >= 3:
        return f"{base} · 已主动召回 {recalls} 次（低频抽检）"
    if recalls > 0:
        return f"{base} · 近30天召回 {recalls} 次"
    if skips > 0:
        return f"{base} · 已轻度降权"
    return f"{base} · 尚未主动召回"


def lens_to_seed_dict(lens: dict[str, Any], layer: str = "L4") -> dict[str, Any]:
    """Extract a clean, display-ready SeedItem payload from an L4 or L3 lens dict."""
    routing = lens.get("routing") or {}
    execution = lens.get("execution") or {}
    meta = lens.get("meta") or {}
    triggers = [str(t).strip() for t in (routing.get("trigger_utterances") or []) if str(t).strip()]
    return {
        "id": str(lens.get("id", "")),
        "title": str(lens.get("title", "")),
        "tagline": str(lens.get("tagline") or routing.get("pain_point") or ""),
        "pain_point": str(routing.get("pain_point") or lens.get("tagline") or ""),
        "prompt": str(execution.get("content") or ""),
        "primitive": str(routing.get("primitive") or "metacognize"),
        "category": str(meta.get("category") or "general"),
        "tier": str(meta.get("tier") or ("A" if layer == "L3" else "B")),
        "layer": layer,
        "source": str(meta.get("source") or ""),
        "trigger_utterances": triggers[:4],
    }
