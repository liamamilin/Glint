"""Stage 3 Pipeline: L2 Draft Lenses -> L3 Production Lenses (Deterministic Cleaning, Transform & Dedup).

Zero LLM calls. Re-runnable in seconds whenever cleaning rules, domain aliases,
dedup thresholds, or local overlays change. Never mutates L2 draft files.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("glint.l2_to_l3")

import numpy as np
import yaml

from pipeline.embed_client import Embedder
from pipeline.l1_to_l2 import load_existing_l2_lenses
from pipeline.ontology import (
    LIB_ROOT,
    Ontology,
    extract_placeholders,
    validate_placeholders,
)
from pipeline.schema import L2Lens, OverlayFile, QuarantineRecord, WorkflowSpec, make_stable_id

DEFAULT_CLEANING_RULES_PATH = LIB_ROOT / "config" / "cleaning_rules.yaml"
DEFAULT_SHELF_POLICY_PATH = LIB_ROOT / "config" / "shelf_policy.yaml"
DEFAULT_TAXONOMY_PATH = LIB_ROOT / "config" / "taxonomy.yaml"
DEFAULT_PT_SNAPSHOT_PATH = LIB_ROOT / "data" / "l1_materials" / "external" / "prompt-taxonomy" / "_raw_snapshot.json"
INPUT_PLACEHOLDER_RE = re.compile(r"\s*\{\{\s*input\s*\}\}\s*")


def load_taxonomy(path: Path = DEFAULT_TAXONOMY_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def classify_lens_taxonomy(lens: L2Lens, taxonomy: dict[str, Any]) -> tuple[str, str]:
    """Deterministically assign (category, subcategory) from taxonomy.yaml rules."""
    rules = taxonomy.get("classification_rules") or []
    # Pass 1: Match against explicit domain tags first (highest precision)
    domain_haystack = " ".join(lens.routing.domains).lower()
    if domain_haystack:
        for rule in rules:
            kws = [str(k).lower() for k in (rule.get("keywords") or [])]
            if any(kw in domain_haystack for kw in kws):
                return str(rule["category"]), str(rule["subcategory"])
    # Pass 2: Match against title, tagline, and pain_point (never meta.source!)
    text_haystack = " ".join([lens.title, lens.tagline, lens.routing.pain_point]).lower()
    for rule in rules:
        kws = [str(k).lower() for k in (rule.get("keywords") or [])]
        if any(kw in text_haystack for kw in kws):
            return str(rule["category"]), str(rule["subcategory"])
    # Fallback by primitive
    prim_default = {
        "critique": ("general", "critical_thinking"),
        "converge": ("general", "decision"),
        "translate": ("writing_comms", "writing_polish"),
        "compress": ("general", "research"),
        "metacognize": ("general", "problem_solving"),
        "diverge": ("general", "problem_solving"),
        "deconstruct": ("general", "problem_solving"),
    }
    return prim_default.get(lens.routing.primitive, ("general", "problem_solving"))


def load_workflow_map(snapshot_path: Path = DEFAULT_PT_SNAPSHOT_PATH) -> dict[str, WorkflowSpec]:
    """Build deterministic lens_id -> WorkflowSpec map from Prompt-taxonomy workflows."""
    if not snapshot_path.exists():
        return {}
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    wf_map: dict[str, WorkflowSpec] = {}
    for wf in payload.get("workflows", []):
        wf_id = str(wf.get("id", "")).strip()
        steps = [str(s).strip() for s in (wf.get("steps") or []) if str(s).strip()]
        if not wf_id or len(steps) < 2:
            continue
        lens_ids = [make_stable_id("pt", s) for s in steps]
        for idx, lid in enumerate(lens_ids):
            if lid in wf_map:
                continue
            next_id = lens_ids[idx + 1] if idx + 1 < len(lens_ids) else None
            wf_map[lid] = WorkflowSpec(workflow_id=wf_id, step=idx + 1, next_lens_id=next_id)
    return wf_map


def load_cleaning_rules(path: Path = DEFAULT_CLEANING_RULES_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_shelf_policy(path: Path = DEFAULT_SHELF_POLICY_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_overlay(overlay_path: Path, meta_vault_dir: Path | None = None) -> OverlayFile:
    base = OverlayFile()
    if overlay_path.exists():
        raw = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
        base = OverlayFile.model_validate(raw)
    if meta_vault_dir is not None:
        v_ov_path = meta_vault_dir / "overrides.yaml"
        if v_ov_path.exists():
            v_raw = yaml.safe_load(v_ov_path.read_text(encoding="utf-8")) or {}
            v_ov = OverlayFile.model_validate(v_raw)
            merged = dict(base.overrides)
            merged.update(v_ov.overrides)
            base = OverlayFile(overrides=merged)
    return base


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )


def _strip_boilerplate(text: str, patterns: list[re.Pattern[str]]) -> tuple[str, bool]:
    cleaned = text.strip()
    changed = False
    for pat in patterns:
        new_text = pat.sub("", cleaned).strip()
        if new_text and new_text != cleaned:
            cleaned = new_text
            changed = True
    return cleaned, changed


def _normalize_domain_token(token: str, alias_map: dict[str, str]) -> str:
    raw = token.strip()
    if not raw:
        return ""
    if raw in alias_map:
        return alias_map[raw]
    lower = raw.lower()
    if lower in alias_map:
        return alias_map[lower]
    # Normalize ASCII spaces/hyphens to snake_case while preserving Chinese if unmapped
    if re.fullmatch(r"[a-zA-Z0-9_\-\s/]+", lower):
        norm = re.sub(r"[\s\-/]+", "_", lower).strip("_")
        return alias_map.get(norm, norm)
    return raw


def _normalize_app_token(token: str, alias_map: dict[str, str]) -> str:
    raw = token.strip()
    if not raw:
        return ""
    return alias_map.get(raw.lower(), alias_map.get(raw, raw))


@dataclass
class L3CleanReport:
    generated_at: str
    total_l2_input: int = 0
    total_l3_active: int = 0
    total_l3_deprecated: int = 0
    total_quarantined_rules: int = 0
    total_quarantined_dedup: int = 0
    action_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    dedup_conflicts: list[dict[str, Any]] = field(default_factory=list)
    embedding_model: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "total_l2_input": self.total_l2_input,
            "total_l3_active": self.total_l3_active,
            "total_l3_deprecated": self.total_l3_deprecated,
            "total_quarantined_rules": self.total_quarantined_rules,
            "total_quarantined_dedup": self.total_quarantined_dedup,
            "action_counts": self.action_counts,
            "warnings_count": len(self.warnings),
            "warnings": self.warnings[:50],
            "dedup_conflicts": self.dedup_conflicts,
            "embedding_model": self.embedding_model,
        }


def clean_single_lens(
    draft: L2Lens,
    *,
    ontology: Ontology,
    rules: dict[str, Any],
    overlay: OverlayFile,
    shelf_policy: dict[str, Any] | None = None,
    taxonomy: dict[str, Any] | None = None,
    bp_patterns: list[re.Pattern[str]] | None = None,
    cn_var_pattern: re.Pattern[str] | None = None,
) -> tuple[L2Lens | None, list[str], list[str], list[str]]:
    """Run Passes 1-5 on one L2 draft.

    Returns (cleaned_lens_or_None, actions_taken, warnings, fatal_reasons).
    """
    lens = draft.model_copy(deep=True)
    actions: list[str] = []
    warnings: list[str] = []
    reasons: list[str] = []

    # Pass 1: Enum & Alias Normalization
    norm_prim = ontology.normalize_primitive(lens.routing.primitive)
    if norm_prim is None:
        reasons.append(f"invalid_primitive:{lens.routing.primitive}")
    elif norm_prim != lens.routing.primitive:
        lens.routing.primitive = norm_prim
        actions.append("normalized_primitive")

    goals, unknown_goals = ontology.normalize_list("goals", lens.routing.goals, ontology.goals)
    if unknown_goals:
        reasons.append(f"invalid_goals:{','.join(unknown_goals)}")
    elif not goals:
        reasons.append("missing_goals")
    else:
        if goals != lens.routing.goals:
            actions.append("normalized_goals")
        lens.routing.goals = goals

    clarities, unknown_clarities = ontology.normalize_list(
        "clarities", lens.routing.clarity_fit, ontology.clarities
    )
    if unknown_clarities:
        reasons.append(f"invalid_clarities:{','.join(unknown_clarities)}")
    elif not clarities:
        reasons.append("missing_clarity_fit")
    else:
        if clarities != lens.routing.clarity_fit:
            actions.append("normalized_clarities")
        lens.routing.clarity_fit = clarities

    norm_action = ontology.normalize_action_type(lens.execution.action_type)
    if norm_action is None:
        reasons.append(f"invalid_action_type:{lens.execution.action_type}")
    elif norm_action != lens.execution.action_type:
        lens.execution.action_type = norm_action
        actions.append("normalized_action_type")

    # Pass 2: Domain & App Affinity Normalization
    domain_aliases = {str(k).lower(): str(v) for k, v in (rules.get("domain_aliases") or {}).items()}
    new_domains = list(
        dict.fromkeys(
            d for d in (_normalize_domain_token(x, domain_aliases) for x in lens.routing.domains) if d
        )
    )
    if new_domains != lens.routing.domains:
        lens.routing.domains = new_domains
        actions.append("normalized_domains")

    app_aliases = {str(k).lower(): str(v) for k, v in (rules.get("app_aliases") or {}).items()}
    new_apps = list(
        dict.fromkeys(
            a for a in (_normalize_app_token(x, app_aliases) for x in lens.routing.app_affinity) if a
        )
    )
    if new_apps != lens.routing.app_affinity:
        lens.routing.app_affinity = new_apps
        actions.append("normalized_app_affinity")

    # Pass 3: Template, Placeholder & Person/Context Hygiene
    content = lens.execution.content.strip()
    if lens.execution.action_type == "inject_prompt":
        if lens.execution.inject_mode == "append" and INPUT_PLACEHOLDER_RE.search(content):
            content = INPUT_PLACEHOLDER_RE.sub(" ", content).strip()
            actions.append("stripped_append_input_placeholder")
        elif lens.execution.inject_mode == "wrap" and "input" not in extract_placeholders(content):
            content = content + "\n\n{{input}}"
            actions.append("auto_appended_wrap_input_placeholder")
        person_repls = rules.get("execution_person_replacements") or {}
        for old_phrase, new_phrase in person_repls.items():
            if old_phrase in content:
                content = content.replace(str(old_phrase), str(new_phrase))
                if "normalized_person_context" not in actions:
                    actions.append("normalized_person_context")
    lens.execution.content = content

    # Extract {{var}} and Chinese 【var】 placeholders into execution.variables
    detected_curly = [v for v in extract_placeholders(content) if v != "input"]
    compiled_cn_pat = cn_var_pattern
    if compiled_cn_pat is None and rules.get("chinese_variable_pattern"):
        compiled_cn_pat = re.compile(str(rules["chinese_variable_pattern"]))
    detected_cn: list[str] = []
    if compiled_cn_pat is not None:
        for m in compiled_cn_pat.finditer(content):
            var_name = m.group(1).strip()
            if var_name and var_name not in detected_cn:
                detected_cn.append(var_name)
    merged_vars = list(
        dict.fromkeys(
            [v.strip() for v in lens.execution.variables if v.strip() and v.strip() != "input"]
            + detected_curly
            + detected_cn
        )
    )
    if merged_vars != lens.execution.variables:
        lens.execution.variables = merged_vars
        actions.append("synced_variables")

    # Pass 4: Expression Polish
    strip_chars = str(rules.get("title_strip_chars", " 。，、；："))
    cleaned_title = lens.title.strip(strip_chars).strip()
    if cleaned_title and cleaned_title != lens.title:
        lens.title = cleaned_title
        actions.append("cleaned_title_punctuation")
    title_max = int(rules.get("title_max_chars", 18))
    if len(lens.title) > title_max:
        warnings.append(f"title_too_long:{len(lens.title)}>{title_max}")

    compiled_bp = (
        bp_patterns
        if bp_patterns is not None
        else [re.compile(p) for p in (rules.get("meta_boilerplate_prefixes") or [])]
    )
    cleaned_pain, pain_changed = _strip_boilerplate(lens.routing.pain_point, compiled_bp)
    if pain_changed:
        lens.routing.pain_point = cleaned_pain
        actions.append("stripped_pain_boilerplate")
    cleaned_tag, tag_changed = _strip_boilerplate(lens.tagline, compiled_bp)
    if tag_changed:
        lens.tagline = cleaned_tag
        actions.append("stripped_tagline_boilerplate")

    # Clean trigger_utterances (strip quotes, deduplicate case-insensitively)
    seen_trig: set[str] = set()
    clean_triggers: list[str] = []
    for t in lens.routing.trigger_utterances:
        ct = t.strip().strip("“”\"'‘’").strip()
        if not ct:
            continue
        key = ct.lower()
        if key not in seen_trig:
            seen_trig.add(key)
            clean_triggers.append(ct)
    if clean_triggers != lens.routing.trigger_utterances:
        lens.routing.trigger_utterances = clean_triggers
        actions.append("cleaned_triggers")

    min_triggers = int(rules.get("min_trigger_utterances", 2))
    if len(lens.routing.trigger_utterances) < min_triggers:
        reasons.append(f"insufficient_triggers:{len(lens.routing.trigger_utterances)}<{min_triggers}")

    min_conf = float(rules.get("min_confidence", 0.60))
    if lens.meta.confidence < min_conf:
        reasons.append(f"low_confidence:{lens.meta.confidence:.2f}<{min_conf:.2f}")

    # Final placeholder syntax check
    ph_errs = validate_placeholders(
        lens.execution.content,
        lens.execution.action_type,
        lens.execution.inject_mode,
    )
    reasons.extend(ph_errs)

    if reasons:
        return None, actions, warnings, reasons

    # Pass 4b: Assign 2-level Taxonomy (category/subcategory), Tier (S/A/B), Collections & Declarative L4 Shelf
    tax = taxonomy or {}
    is_user_authored = (
        lens.meta.source in {"manual-custom", "cle-v0.7"}
        or lens.meta.source.startswith("forge-")
    )
    valid_cats = tax.get("categories") or {}
    if is_user_authored and lens.meta.category in valid_cats:
        subcats = (valid_cats[lens.meta.category] or {}).get("subcategories") or {}
        if lens.meta.subcategory not in subcats:
            lens.meta.subcategory = next(iter(subcats), "general_custom")
    else:
        cat, subcat = classify_lens_taxonomy(lens, tax)
        lens.meta.category = cat
        lens.meta.subcategory = subcat

    sp = shelf_policy or {}
    evergreen_ids = set(sp.get("evergreen_ids") or [])
    off_shelf_sources = set(sp.get("off_shelf_sources") or [])
    tier_a_sources = set(sp.get("tier_a_sources") or [])
    tier_a_min_conf = float(sp.get("tier_a_min_confidence", 0.88))
    category_modes = sp.get("category_modes") or {}
    active_collections = set(sp.get("active_collections") or [])
    collections_cfg = sp.get("collections") or {}

    # Collect custom collections containing this lens
    member_cols = [
        cid for cid, cinfo in collections_cfg.items()
        if isinstance(cinfo, dict) and lens.id in (cinfo.get("lens_ids") or [])
    ]
    lens.meta.collections = sorted(set(member_cols))

    # Tier assignment: S (Evergreen) > A (High-Signal) > B (Long-Tail)
    if lens.id in evergreen_ids or (is_user_authored and lens.meta.tier == "S"):
        lens.meta.tier = "S"
        lens.meta.scope = "universal"
    elif is_user_authored and lens.meta.tier in {"A", "B"}:
        lens.meta.scope = "domain_specific"
    elif lens.meta.source in tier_a_sources or lens.meta.confidence >= tier_a_min_conf:
        lens.meta.tier = "A"
        lens.meta.scope = "domain_specific"
    else:
        lens.meta.tier = "B"
        lens.meta.scope = "domain_specific"

    # Pass 5: Local Immutable Overlay Merge (applied before computing final declarative shelf,
    # unless ov.shelf explicitly pins the shelf)
    ov = overlay.overrides.get(lens.id)
    if ov is not None:
        if ov.title is not None:
            lens.title = ov.title
        if ov.tagline is not None:
            lens.tagline = ov.tagline
        if ov.status is not None:
            lens.meta.status = ov.status
        if ov.tier is not None:
            lens.meta.tier = ov.tier
            if ov.tier == "S":
                lens.meta.scope = "universal"
        if ov.category is not None:
            lens.meta.category = ov.category
        if ov.subcategory is not None:
            lens.meta.subcategory = ov.subcategory
        if ov.collections is not None:
            lens.meta.collections = sorted(set(ov.collections))
        if ov.scope is not None:
            lens.meta.scope = ov.scope
        if ov.base_weight is not None:
            lens.meta.base_weight = ov.base_weight
        if ov.primitive is not None:
            ov_prim = ontology.normalize_primitive(ov.primitive)
            if ov_prim is None:
                return None, actions, warnings, [f"invalid_overlay_primitive:{ov.primitive}"]
            lens.routing.primitive = ov_prim
        if ov.domains is not None:
            lens.routing.domains = ov.domains
        if ov.app_affinity is not None:
            lens.routing.app_affinity = ov.app_affinity
        if ov.add_trigger_utterances:
            merged_trigs = list(lens.routing.trigger_utterances)
            for t in ov.add_trigger_utterances:
                t_clean = t.strip()
                if t_clean and t_clean not in merged_trigs:
                    merged_trigs.append(t_clean)
            lens.routing.trigger_utterances = merged_trigs
        if ov.superseded_by is not None:
            lens.meta.superseded_by = ov.superseded_by
        if ov.evolution_note is not None:
            lens.meta.evolution_note = ov.evolution_note
        actions.append("applied_overlay")

    # Declarative L4 Shelf computation:
    #   S-tier evergreen (always on) ∪ active_collections ∪ category_modes(off|tier_a|all)
    if ov is not None and ov.shelf is not None:
        lens.meta.shelf = ov.shelf
    elif lens.meta.tier == "S" and sp.get("always_on_tier_s", True):
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

    return lens, actions, warnings, []


def _merge_vault_shelf_policy(shelf_policy: dict[str, Any], meta_vault_dir: Path | None) -> dict[str, Any]:
    if meta_vault_dir is None:
        return shelf_policy
    vault_sp_path = meta_vault_dir / "shelf_policy.yaml"
    if not vault_sp_path.exists():
        return shelf_policy
    vault_sp = yaml.safe_load(vault_sp_path.read_text(encoding="utf-8")) or {}
    merged = dict(shelf_policy)

    base_evergreen = list(shelf_policy.get("evergreen_ids") or [])
    for eid in vault_sp.get("evergreen_ids") or []:
        if eid not in base_evergreen:
            base_evergreen.append(eid)
    merged["evergreen_ids"] = base_evergreen

    merged_cols: dict[str, Any] = {}
    for cid, cinfo in (shelf_policy.get("collections") or {}).items():
        if isinstance(cinfo, dict):
            entry = dict(cinfo)
            entry.setdefault("is_vault", False)
            merged_cols[cid] = entry
    for cid, cinfo in (vault_sp.get("collections") or {}).items():
        if isinstance(cinfo, dict):
            entry = dict(cinfo)
            entry["is_vault"] = True
            merged_cols[cid] = entry
    merged["collections"] = merged_cols

    base_active = list(shelf_policy.get("active_collections") or [])
    for cid in vault_sp.get("active_collections") or []:
        if cid not in base_active:
            base_active.append(cid)
    merged["active_collections"] = base_active
    return merged


def _materialize_l3_dir(
    target_dir: Path,
    lenses: list[L2Lens],
    report_dict: dict[str, Any] | None = None,
) -> None:
    staging_dir = target_dir.parent / f".{target_dir.name}.staging.{os.getpid()}.{time.time_ns()}"
    backup_dir = target_dir.parent / f".{target_dir.name}.old.{os.getpid()}.{time.time_ns()}"
    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
        for lens in lenses:
            _write_yaml(staging_dir / lens.routing.primitive / f"{lens.id}.yaml", lens.model_dump())
        if report_dict is not None:
            (staging_dir / "cleaning_report.json").write_text(
                json.dumps(report_dict, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if target_dir.exists():
            target_dir.rename(backup_dir)
        staging_dir.rename(target_dir)
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
    except BaseException:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        if not target_dir.exists() and backup_dir.exists():
            backup_dir.rename(target_dir)
        raise


def run_l2_to_l3(
    *,
    l2_dir: Path,
    l3_dir: Path,
    overlay_path: Path,
    quarantine_dir: Path,
    ontology: Ontology | None = None,
    rules_path: Path = DEFAULT_CLEANING_RULES_PATH,
    shelf_policy_path: Path = DEFAULT_SHELF_POLICY_PATH,
    embedder: Embedder | None = None,
    meta_vault_dir: Path | None = None,
) -> tuple[list[L2Lens], np.ndarray | None, L3CleanReport]:
    """Execute full deterministic L2 -> L3 cleaning, transformation, and priority dedup.

    Optional `meta_vault_dir` lenses are cleaned & merged into the returned runtime inventory,
    while being materialized strictly into `meta_vault_dir/l3_production` so public `l3_dir`
    and `cleaning_report.json` never contain private vault items.
    """
    ont = ontology or Ontology.load()
    rules = load_cleaning_rules(rules_path)
    shelf_policy = _merge_vault_shelf_policy(load_shelf_policy(shelf_policy_path), meta_vault_dir)
    taxonomy = load_taxonomy()
    overlay = _load_overlay(overlay_path, meta_vault_dir=meta_vault_dir)
    l2_map = load_existing_l2_lenses(l2_dir)
    public_ids = set(l2_map.keys())
    vault_ids: set[str] = set()
    if meta_vault_dir is not None:
        vault_l2_dir = meta_vault_dir / "l2_lenses"
        if vault_l2_dir.exists():
            vault_map = load_existing_l2_lenses(vault_l2_dir)
            vault_ids = set(vault_map.keys())
            dup_ids = sorted(public_ids & vault_ids)
            if dup_ids:
                raise ValueError(
                    "Duplicate lens IDs between public l2_lenses and meta-vault/l2_lenses: "
                    + ", ".join(dup_ids)
                )
            l2_map.update(vault_map)
    workflow_map = load_workflow_map()

    report = L3CleanReport(
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        total_l2_input=len(public_ids),
        embedding_model=embedder.model if embedder is not None else "none",
    )
    action_counter: Counter[str] = Counter()
    public_action_counter: Counter[str] = Counter()

    # Clear previous L3 quarantine outputs for clean idempotent re-runs
    q_rules_dir = quarantine_dir / "l3_rules"
    q_dedup_dir = quarantine_dir / "l3_dedup"
    for d in (q_rules_dir, q_dedup_dir):
        if d.exists():
            shutil.rmtree(d)

    vq_rules_dir = (meta_vault_dir / "quarantine" / "l3_rules") if meta_vault_dir is not None else None
    vq_dedup_dir = (meta_vault_dir / "quarantine" / "l3_dedup") if meta_vault_dir is not None else None
    for d in (vq_rules_dir, vq_dedup_dir):
        if d is not None and d.exists():
            shutil.rmtree(d)

    candidates: list[L2Lens] = []
    deprecated_lenses: list[L2Lens] = []

    compiled_bp = [re.compile(p) for p in (rules.get("meta_boilerplate_prefixes") or [])]
    compiled_cn_pat = (
        re.compile(str(rules["chinese_variable_pattern"]))
        if rules.get("chinese_variable_pattern")
        else None
    )

    for lens_id in sorted(l2_map.keys()):
        is_vault = lens_id in vault_ids
        draft = l2_map[lens_id]
        cleaned, actions, warnings, reasons = clean_single_lens(
            draft,
            ontology=ont,
            rules=rules,
            overlay=overlay,
            shelf_policy=shelf_policy,
            taxonomy=taxonomy,
            bp_patterns=compiled_bp,
            cn_var_pattern=compiled_cn_pat,
        )
        for act in actions:
            action_counter[act] += 1
            if not is_vault:
                public_action_counter[act] += 1
        if warnings:
            if is_vault:
                logger.warning("Vault lens '%s' warnings: %s", lens_id, "; ".join(warnings))
            else:
                report.warnings.append({"id": lens_id, "title": draft.title, "warnings": warnings})

        if cleaned is None:
            rec = QuarantineRecord(
                id=draft.id,
                source=draft.meta.source,
                source_ref=draft.meta.source_ref,
                source_hash=draft.meta.source_hash,
                reasons=reasons,
                draft=draft.model_dump(),
            )
            if is_vault and vq_rules_dir is not None:
                logger.warning("Vault lens '%s' failed L3 rule gate: %s", draft.id, ", ".join(reasons))
                _write_yaml(vq_rules_dir / f"{draft.id}.yaml", rec.model_dump())
            else:
                _write_yaml(q_rules_dir / f"{draft.id}.yaml", rec.model_dump())
                report.total_quarantined_rules += 1
            continue

        if cleaned.meta.status == "deprecated":
            if not is_vault:
                deprecated_lenses.append(cleaned)
        else:
            candidates.append(cleaned)

    # Pass 6: Priority-ordered cross-source semantic dedup via bge-m3
    source_priority = {str(k): int(v) for k, v in (rules.get("source_priority") or {}).items()}
    dedup_threshold = float(rules.get("semantic_dedup_threshold", 0.88))
    candidates.sort(
        key=lambda l: (-source_priority.get(l.meta.source, 0), -l.meta.confidence, l.id)
    )

    kept_lenses: list[L2Lens] = []
    kept_vectors: np.ndarray | None = None

    if embedder is not None and candidates:
        all_vecs = embedder.embed_texts([l.routing_profile_text() for l in candidates])
        kept_indices: list[int] = []
        for idx, lens in enumerate(candidates):
            vec = all_vecs[idx]
            if kept_indices:
                kept_mat = all_vecs[kept_indices]
                sims = (kept_mat @ vec).reshape(-1)
                best_pos = int(np.argmax(sims))
                best_sim = float(sims[best_pos])
                if best_sim >= dedup_threshold:
                    winner = candidates[kept_indices[best_pos]]
                    is_private_pair = (lens.id in vault_ids) or (winner.id in vault_ids)
                    rec = QuarantineRecord(
                        id=lens.id,
                        source=lens.meta.source,
                        source_ref=lens.meta.source_ref,
                        source_hash=lens.meta.source_hash,
                        reasons=[f"l3_semantic_duplicate:{winner.id}({best_sim:.3f}>={dedup_threshold:.2f})"],
                        duplicate_of=winner.id,
                        duplicate_score=round(best_sim, 4),
                        draft=lens.model_dump(),
                    )
                    if is_private_pair:
                        logger.warning(
                            "Vault-related lens '%s' dropped by L3 semantic dedup against '%s' (%.3f >= %.2f)",
                            lens.id,
                            winner.id,
                            best_sim,
                            dedup_threshold,
                        )
                        if vq_dedup_dir is not None:
                            _write_yaml(vq_dedup_dir / f"{lens.id}.yaml", rec.model_dump())
                    else:
                        _write_yaml(q_dedup_dir / f"{lens.id}.yaml", rec.model_dump())
                        report.total_quarantined_dedup += 1
                        report.dedup_conflicts.append(
                            {
                                "dropped_id": lens.id,
                                "dropped_title": lens.title,
                                "dropped_source": lens.meta.source,
                                "kept_id": winner.id,
                                "kept_title": winner.title,
                                "kept_source": winner.meta.source,
                                "similarity": round(best_sim, 4),
                            }
                        )
                        public_action_counter["dedup_dropped"] += 1
                    action_counter["dedup_dropped"] += 1
                    continue
            kept_indices.append(idx)
            kept_lenses.append(lens)
        kept_vectors = all_vecs[kept_indices] if kept_indices else np.zeros((0, all_vecs.shape[1]), dtype=np.float32)
    else:
        kept_lenses = candidates

    # Attach Prompt-taxonomy multi-step workflow chains where target next_lens_id is present
    kept_ids = {l.id for l in kept_lenses}
    for lens in kept_lenses:
        if lens.execution.workflow is None and lens.id in workflow_map:
            spec = workflow_map[lens.id]
            next_id = spec.next_lens_id if (spec.next_lens_id and spec.next_lens_id in kept_ids) else None
            lens.execution.workflow = WorkflowSpec(
                workflow_id=spec.workflow_id,
                step=spec.step,
                next_lens_id=next_id,
            )
            action_counter["attached_workflow_step"] += 1
            if lens.id not in vault_ids:
                public_action_counter["attached_workflow_step"] += 1

    vault_col_ids = {
        cid
        for cid, cinfo in (shelf_policy.get("collections") or {}).items()
        if isinstance(cinfo, dict) and cinfo.get("is_vault")
    }
    public_kept = [l for l in kept_lenses if l.id not in vault_ids]
    vault_kept = [l for l in kept_lenses if l.id in vault_ids]

    if vault_col_ids:
        public_materialized = []
        for lens in public_kept:
            if any(c in vault_col_ids for c in lens.meta.collections):
                cloned = lens.model_copy(deep=True)
                cloned.meta.collections = [c for c in cloned.meta.collections if c not in vault_col_ids]
                public_materialized.append(cloned)
            else:
                public_materialized.append(lens)
    else:
        public_materialized = public_kept

    report.total_l3_active = len(public_kept)
    report.total_l3_deprecated = len(deprecated_lenses)
    report.action_counts = dict(sorted(public_action_counter.items()))

    # Materialize clean public L3 production directory atomically (never includes vault items or vault collections)
    _materialize_l3_dir(l3_dir, public_materialized, report_dict=report.to_dict())
    if meta_vault_dir is not None:
        vault_l3_dir = meta_vault_dir / "l3_production"
        if vault_kept or vault_l3_dir.exists():
            _materialize_l3_dir(vault_l3_dir, vault_kept, report_dict=None)

    return kept_lenses, kept_vectors, report
