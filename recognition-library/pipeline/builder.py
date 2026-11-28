"""Stage 4 Pipeline: L2->L3 Clean + Overlay Merge + bge-m3 Index Build + Atomic Symlink Release & Rollback."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from pipeline.embed_client import Embedder
from pipeline.l1_to_l2 import load_existing_l2_lenses
from pipeline.l2_to_l3 import DEFAULT_CLEANING_RULES_PATH, load_cleaning_rules, run_l2_to_l3
from pipeline.ontology import Ontology, validate_placeholders
from pipeline.schema import BuildManifest, L2Lens, OverlayFile


def load_overlay(overlay_path: Path, meta_vault_dir: Path | None = None) -> OverlayFile:
    base = OverlayFile()
    if overlay_path.exists():
        raw = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
        base = OverlayFile.model_validate(raw)
    if meta_vault_dir is not None:
        vault_ov_path = meta_vault_dir / "overrides.yaml"
        if vault_ov_path.exists():
            v_raw = yaml.safe_load(vault_ov_path.read_text(encoding="utf-8")) or {}
            vault_ov = OverlayFile.model_validate(v_raw)
            merged = dict(base.overrides)
            merged.update(vault_ov.overrides)
            base = OverlayFile(overrides=merged)
    return base


def apply_overlays(
    lenses: dict[str, L2Lens],
    overlay: OverlayFile,
    ontology: Ontology,
) -> list[L2Lens]:
    """Merge immutable local overrides without mutating source L2 YAML files."""
    merged: list[L2Lens] = []
    for lens_id in sorted(lenses.keys()):
        lens = lenses[lens_id].model_copy(deep=True)
        ov = overlay.overrides.get(lens_id)
        if ov is not None:
            if ov.title is not None:
                lens.title = ov.title
            if ov.tagline is not None:
                lens.tagline = ov.tagline
            if ov.pain_point is not None and len(ov.pain_point.strip()) >= 4:
                lens.routing.pain_point = ov.pain_point.strip()
            if ov.content is not None and len(ov.content.strip()) >= 4:
                lens.execution.content = ov.content.strip()
            if ov.status is not None:
                lens.meta.status = ov.status
            if ov.tier is not None:
                lens.meta.tier = ov.tier
            if ov.category is not None:
                lens.meta.category = ov.category
            if ov.subcategory is not None:
                lens.meta.subcategory = ov.subcategory
            if ov.collections is not None:
                lens.meta.collections = sorted(set(ov.collections))
            if ov.scope is not None:
                lens.meta.scope = ov.scope
            if ov.shelf is not None:
                lens.meta.shelf = ov.shelf
            if ov.base_weight is not None:
                lens.meta.base_weight = ov.base_weight
            if ov.primitive is not None:
                norm_p = ontology.normalize_primitive(ov.primitive)
                if norm_p is None:
                    raise ValueError(f"Overlay for {lens_id} has invalid primitive: {ov.primitive}")
                lens.routing.primitive = norm_p
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
            if ov.remove_trigger_utterances:
                rm_set = {t.strip() for t in ov.remove_trigger_utterances if t.strip()}
                kept = [t for t in lens.routing.trigger_utterances if t.strip() not in rm_set]
                if kept:
                    lens.routing.trigger_utterances = kept
            if ov.superseded_by is not None:
                lens.meta.superseded_by = ov.superseded_by
            if ov.evolution_note is not None:
                lens.meta.evolution_note = ov.evolution_note
        merged.append(lens)
    return merged


def validate_l2_repository(
    l2_dir: Path,
    overlay_path: Path,
    ontology: Ontology | None = None,
    meta_vault_dir: Path | None = None,
    rules_path: Path = DEFAULT_CLEANING_RULES_PATH,
) -> tuple[list[L2Lens], list[str]]:
    """Validate all L2 lenses + overlays against Ontology SSOT and core quality gates."""
    ont = ontology or Ontology.load()
    rules = load_cleaning_rules(rules_path)
    min_triggers = int(rules.get("min_trigger_utterances", 2))
    min_conf = float(rules.get("min_confidence", 0.60))

    errors: list[str] = []
    lenses_map = load_existing_l2_lenses(l2_dir)
    if meta_vault_dir is not None:
        vault_l2 = meta_vault_dir / "l2_lenses"
        if vault_l2.exists():
            vault_map = load_existing_l2_lenses(vault_l2)
            dup_ids = sorted(set(lenses_map.keys()) & set(vault_map.keys()))
            for dup_id in dup_ids:
                errors.append(
                    f"{dup_id}: duplicate lens ID exists in both public l2_lenses and meta-vault/l2_lenses"
                )
            lenses_map.update(vault_map)
    overlay = load_overlay(overlay_path, meta_vault_dir=meta_vault_dir)

    try:
        merged = apply_overlays(lenses_map, overlay, ont)
    except Exception as exc:
        return [], [f"overlay_error:{exc}"]

    for lens in merged:
        if lens.routing.primitive not in ont.primitives:
            errors.append(f"{lens.id}: invalid primitive '{lens.routing.primitive}'")
        for g in lens.routing.goals:
            if g not in ont.goals:
                errors.append(f"{lens.id}: invalid goal '{g}'")
        for c in lens.routing.clarity_fit:
            if c not in ont.clarities:
                errors.append(f"{lens.id}: invalid clarity '{c}'")
        if lens.execution.action_type not in ont.action_types:
            errors.append(f"{lens.id}: invalid action_type '{lens.execution.action_type}'")
        if len(lens.routing.trigger_utterances) < min_triggers:
            errors.append(
                f"{lens.id}: insufficient trigger_utterances ({len(lens.routing.trigger_utterances)} < {min_triggers})"
            )
        if lens.meta.confidence < min_conf:
            errors.append(f"{lens.id}: low confidence ({lens.meta.confidence:.2f} < {min_conf:.2f})")
        ph_errs = validate_placeholders(
            lens.execution.content,
            lens.execution.action_type,
            lens.execution.inject_mode,
        )
        for pe in ph_errs:
            errors.append(f"{lens.id}: {pe}")

    return merged, errors


def _atomic_flip_symlink(link_path: Path, target_rel: str) -> None:
    """Atomically replace `link_path` symlink to point at `target_rel`."""
    link_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_link = link_path.parent / f".{link_path.name}.tmp.{os.getpid()}.{time.time_ns()}"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    os.symlink(target_rel, tmp_link)
    os.replace(tmp_link, link_path)


def build_release(
    *,
    l2_dir: Path,
    overlay_path: Path,
    quarantine_dir: Path,
    dist_dir: Path,
    l3_dir: Path | None = None,
    rules_path: Path = DEFAULT_CLEANING_RULES_PATH,
    ontology: Ontology | None = None,
    embedder: Embedder | None = None,
    meta_vault_dir: Path | None = None,
) -> tuple[Path, BuildManifest]:
    """Run deterministic L2->L3 cleaning, build immutable dist/releases/<ver>/, and atomically flip dist/current."""
    ont = ontology or Ontology.load()
    target_l3_dir = l3_dir or (l2_dir.parent / "l3_production")

    inventory, inv_vectors, clean_report = run_l2_to_l3(
        l2_dir=l2_dir,
        l3_dir=target_l3_dir,
        overlay_path=overlay_path,
        quarantine_dir=quarantine_dir,
        ontology=ont,
        rules_path=rules_path,
        embedder=embedder,
        meta_vault_dir=meta_vault_dir,
    )
    if not inventory:
        raise ValueError("No active L3 inventory lenses found after L2->L3 cleaning")

    # L4 On-Shelf selection: publish evergreen + on_shelf as primary; publish active off_shelf as L3 shadow pool
    on_shelf_indices = [
        idx for idx, lens in enumerate(inventory) if lens.meta.shelf in {"evergreen", "on_shelf"}
    ]
    off_shelf_indices = [
        idx for idx, lens in enumerate(inventory) if lens.meta.shelf == "off_shelf" and lens.meta.status == "active"
    ]
    if not on_shelf_indices:
        raise ValueError("No L4 on-shelf lenses found to publish")

    active = [inventory[i] for i in on_shelf_indices]
    vectors = inv_vectors[on_shelf_indices] if inv_vectors is not None else None
    shadow = [inventory[i] for i in off_shelf_indices]
    shadow_vectors = inv_vectors[off_shelf_indices] if (inv_vectors is not None and off_shelf_indices) else None
    overlay_obj = load_overlay(overlay_path, meta_vault_dir=meta_vault_dir)
    shadow_payload: list[dict[str, Any]] = []
    for lens in shadow:
        row = lens.model_dump()
        ov = overlay_obj.overrides.get(lens.id)
        row["_manual_off_shelf"] = bool(ov is not None and ov.shelf == "off_shelf")
        shadow_payload.append(row)
    counts_by_shelf = dict(sorted(Counter(lens.meta.shelf for lens in inventory).items()))
    counts_by_tier = dict(sorted(Counter(lens.meta.tier for lens in inventory).items()))
    counts_by_category = dict(sorted(Counter(lens.meta.category for lens in inventory).items()))

    quarantined_l2 = len(list(quarantine_dir.glob("*.yaml"))) if quarantine_dir.exists() else 0
    quarantined_total = quarantined_l2 + clean_report.total_quarantined_rules + clean_report.total_quarantined_dedup

    lenses_payload: list[dict[str, Any]] = [lens.model_dump() for lens in active]
    lenses_json_bytes = (json.dumps(lenses_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    lenses_sha = hashlib.sha256(lenses_json_bytes).hexdigest()

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    version = f"{timestamp}_{lenses_sha[:8]}"
    releases_dir = dist_dir / "releases"
    release_dir = releases_dir / version
    staging_dir = releases_dir / f".staging_{version}_{os.getpid()}"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        (staging_dir / "lenses.json").write_bytes(lenses_json_bytes)
        (staging_dir / "l3_shadow_lenses.json").write_text(
            json.dumps(shadow_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (staging_dir / "cleaning_report.json").write_text(
            json.dumps(clean_report.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        embed_model_name = "none"
        embed_dim = 0
        embeddings_sha: str | None = None
        if embedder is not None and vectors is not None:
            embed_model_name = embedder.model
            embed_dim = int(vectors.shape[1]) if vectors.ndim == 2 and vectors.shape[0] > 0 else 0
            npz_path = staging_dir / "embeddings.npz"
            np.savez_compressed(
                npz_path,
                ids=np.array([lens.id for lens in active]),
                vectors=vectors.astype(np.float32),
            )
            embeddings_sha = hashlib.sha256(npz_path.read_bytes()).hexdigest()
            if shadow_vectors is not None and len(shadow) > 0:
                np.savez_compressed(
                    staging_dir / "l3_shadow_embeddings.npz",
                    ids=np.array([lens.id for lens in shadow]),
                    vectors=shadow_vectors.astype(np.float32),
                )

        by_primitive = dict(sorted(Counter(lens.routing.primitive for lens in active).items()))
        by_source = dict(sorted(Counter(lens.meta.source for lens in active).items()))

        manifest = BuildManifest(
            version=version,
            schema_version=ont.schema_version,
            built_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            embedding_model=embed_model_name,
            embedding_dim=embed_dim,
            total_l3_inventory=len(inventory),
            total_active_lenses=len(active),
            total_quarantined=quarantined_total,
            counts_by_shelf=counts_by_shelf,
            counts_by_tier=counts_by_tier,
            counts_by_category=counts_by_category,
            counts_by_primitive=by_primitive,
            counts_by_source=by_source,
            lenses_sha256=lenses_sha,
            embeddings_sha256=embeddings_sha,
        )
        (staging_dir / "manifest.json").write_text(
            manifest.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )

        if release_dir.exists():
            shutil.rmtree(release_dir)
        staging_dir.rename(release_dir)
        _atomic_flip_symlink(dist_dir / "current", f"releases/{version}")
        return release_dir, manifest
    except BaseException:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def rollback_release(dist_dir: Path, target_version: str | None = None) -> str:
    """Atomically roll back dist/current to the previous (or specified) release version."""
    releases_dir = dist_dir / "releases"
    current_link = dist_dir / "current"
    if not releases_dir.exists():
        raise FileNotFoundError(f"No releases directory at {releases_dir}")

    versions = sorted(
        p.name for p in releases_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    if not versions:
        raise ValueError("No releases available to roll back to")

    current_version: str | None = None
    if current_link.is_symlink():
        current_version = Path(os.readlink(current_link)).name

    if target_version is not None:
        if target_version not in versions:
            raise ValueError(f"Release version '{target_version}' not found in {versions}")
        chosen = target_version
    else:
        candidates = [v for v in versions if v != current_version]
        if not candidates:
            raise ValueError("No previous release available for rollback")
        chosen = candidates[-1]

    _atomic_flip_symlink(current_link, f"releases/{chosen}")
    return chosen
