"""Unified CLI for Glint's three-layer cognitive supply chain (L0 -> L1 -> L2 -> dist/current)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from pipeline.adapters.atlas_l0 import load_atlas_l0_sample
from pipeline.adapters.l1_sources import (
    fetch_discovery_taxonomy_l1,
    fetch_prompt_taxonomy_l1,
    fetch_super_individual_toolbox_l1,
    load_glint_core_l1,
    load_l1_directory,
)
from pipeline.builder import build_release, rollback_release, validate_l2_repository
from pipeline.embed_client import DEFAULT_EMBED_MODEL, OllamaEmbedClient
from pipeline.l0_to_l1 import run_l0_to_l1
from pipeline.l1_to_l2 import compile_l1_to_l2, load_existing_l2_lenses
from pipeline.l2_to_l3 import run_l2_to_l3
from pipeline.ontology import LIB_ROOT, Ontology, load_policy, load_sources
from pipeline.opencode_client import DEFAULT_MODEL, OpenCodeGoClient
from pipeline.schema import L1Material

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("glint.cli")


def _resolve(rel_or_abs: str) -> Path:
    p = Path(rel_or_abs)
    return p if p.is_absolute() else (LIB_ROOT / p).resolve()


def cmd_fetch_l1(args: argparse.Namespace) -> int:
    sources_cfg = load_sources().get("l1_sources", {})
    targets = list(sources_cfg.keys()) if args.source == "all" else [args.source]

    for key in targets:
        cfg = sources_cfg.get(key)
        if not cfg or not cfg.get("enabled", True):
            print(f"[skip] {key} (disabled or unknown)")
            continue
        adapter = cfg["adapter"]
        snapshot_dir = _resolve(cfg["snapshot_dir"])
        if adapter == "glint_cards_yaml":
            mats = load_glint_core_l1(_resolve(cfg["path"]), snapshot_dir=snapshot_dir)
        elif adapter == "prompt_taxonomy_web":
            mats = fetch_prompt_taxonomy_l1(cfg["base_url"], snapshot_dir=snapshot_dir)
        elif adapter == "discovery_taxonomy_web":
            mats = fetch_discovery_taxonomy_l1(cfg["base_url"], snapshot_dir=snapshot_dir)
        elif adapter == "super_individual_toolbox_web":
            mats = fetch_super_individual_toolbox_l1(cfg["base_url"], snapshot_dir=snapshot_dir)
        else:
            print(f"[warn] Unknown L1 adapter '{adapter}' for {key}")
            continue
        print(f"[ok] {key}: fetched {len(mats)} L1 materials -> {snapshot_dir}")
    return 0


def cmd_ingest_l0(args: argparse.Namespace) -> int:
    l0_cfg = load_sources().get("l0_sources", {}).get(args.source)
    if not l0_cfg:
        print(f"Unknown L0 source: {args.source}", file=sys.stderr)
        return 1
    jsonl_path = Path(args.path) if args.path else Path(l0_cfg["path"])
    items = load_atlas_l0_sample(jsonl_path, limit=args.limit, offset=args.offset)
    print(f"Loaded {len(items)} L0 items from {jsonl_path} (offset={args.offset})")

    llm = OpenCodeGoClient(model=args.model, cwd=LIB_ROOT)
    summary = run_l0_to_l1(
        items,
        llm=llm,
        out_dir=LIB_ROOT / "data" / "l1_materials" / "atlas_refined",
        quarantine_dir=LIB_ROOT / "data" / "quarantine",
        workers=args.workers,
        force=args.force,
    )
    print(json.dumps(summary.__dict__, ensure_ascii=False, indent=2))
    return 0 if summary.failed == 0 else 1


def _load_l1_by_source(source_key: str) -> list[L1Material]:
    sources_cfg = load_sources().get("l1_sources", {})
    if source_key == "atlas_refined":
        return load_l1_directory(LIB_ROOT / "data" / "l1_materials" / "atlas_refined")
    if source_key == "all":
        all_mats: list[L1Material] = []
        for k, cfg in sources_cfg.items():
            if cfg.get("enabled", True):
                all_mats.extend(load_l1_directory(_resolve(cfg["snapshot_dir"])))
        all_mats.extend(load_l1_directory(LIB_ROOT / "data" / "l1_materials" / "atlas_refined"))
        return all_mats
    cfg = sources_cfg.get(source_key)
    if not cfg:
        raise ValueError(f"Unknown L1 source '{source_key}'")
    return load_l1_directory(_resolve(cfg["snapshot_dir"]))


def cmd_compile_l2(args: argparse.Namespace) -> int:
    materials = _load_l1_by_source(args.source)
    if args.limit is not None:
        materials = materials[: args.limit]
    if not materials:
        print(f"No L1 materials found for source '{args.source}'. Run fetch-l1 or ingest-l0 first.")
        return 1

    policy = load_policy()
    embed_model = policy.get("models", {}).get("embedding_model", DEFAULT_EMBED_MODEL)
    ollama_url = policy.get("models", {}).get("ollama_base_url", "http://127.0.0.1:11434")

    embedder = None
    if not args.no_dedup:
        client = OllamaEmbedClient(model=embed_model, base_url=ollama_url)
        if client.is_available():
            embedder = client
        else:
            logger.warning("Ollama %s unreachable; skipping Gate 3 semantic dedup", ollama_url)

    llm = OpenCodeGoClient(model=args.model, cwd=LIB_ROOT)
    summary = compile_l1_to_l2(
        materials,
        llm=llm,
        l2_dir=LIB_ROOT / "data" / "l2_lenses",
        quarantine_dir=LIB_ROOT / "data" / "quarantine",
        embedder=embedder,
        workers=args.workers,
        force=args.force,
    )
    print(json.dumps(summary.__dict__, ensure_ascii=False, indent=2))
    return 0 if summary.failed == 0 else 1


def cmd_validate(_: argparse.Namespace) -> int:
    merged, errors = validate_l2_repository(
        LIB_ROOT / "data" / "l2_lenses",
        LIB_ROOT / "data" / "overlays" / "overrides.yaml",
        Ontology.load(),
    )
    if errors:
        print("VALIDATION FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"VALIDATION OK: {len(merged)} lenses verified against ontology.yaml")
    return 0


def cmd_clean_l3(args: argparse.Namespace) -> int:
    policy = load_policy()
    embed_model = policy.get("models", {}).get("embedding_model", DEFAULT_EMBED_MODEL)
    ollama_url = policy.get("models", {}).get("ollama_base_url", "http://127.0.0.1:11434")
    embedder = None
    if not args.no_dedup:
        client = OllamaEmbedClient(model=embed_model, base_url=ollama_url)
        if client.is_available():
            embedder = client
        else:
            logger.warning("Ollama %s unreachable; running L2->L3 without semantic dedup", ollama_url)

    _, _, report = run_l2_to_l3(
        l2_dir=LIB_ROOT / "data" / "l2_lenses",
        l3_dir=LIB_ROOT / "data" / "l3_production",
        overlay_path=LIB_ROOT / "data" / "overlays" / "overrides.yaml",
        quarantine_dir=LIB_ROOT / "data" / "quarantine",
        embedder=embedder,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    policy = load_policy()
    embed_model = policy.get("models", {}).get("embedding_model", DEFAULT_EMBED_MODEL)
    ollama_url = policy.get("models", {}).get("ollama_base_url", "http://127.0.0.1:11434")
    embedder = None if args.no_embed else OllamaEmbedClient(model=embed_model, base_url=ollama_url)

    release_dir, manifest = build_release(
        l2_dir=LIB_ROOT / "data" / "l2_lenses",
        l3_dir=LIB_ROOT / "data" / "l3_production",
        overlay_path=LIB_ROOT / "data" / "overlays" / "overrides.yaml",
        quarantine_dir=LIB_ROOT / "data" / "quarantine",
        dist_dir=LIB_ROOT / "dist",
        embedder=embedder,
    )
    print(f"Built atomic release: {release_dir}")
    print(manifest.model_dump_json(indent=2))
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    chosen = rollback_release(LIB_ROOT / "dist", target_version=args.version)
    print(f"Rolled back dist/current -> releases/{chosen}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    l2 = load_existing_l2_lenses(LIB_ROOT / "data" / "l2_lenses")
    q_dir = LIB_ROOT / "data" / "quarantine"
    q_l2 = len(list(q_dir.glob("*.yaml"))) if q_dir.exists() else 0
    q_l0 = len(list((q_dir / "l0_rejected").glob("*.json"))) if (q_dir / "l0_rejected").exists() else 0
    current_manifest = LIB_ROOT / "dist" / "current" / "manifest.json"
    report = {
        "l2_lenses_total": len(l2),
        "quarantined_l2": q_l2,
        "rejected_l0": q_l0,
        "current_release": json.loads(current_manifest.read_text(encoding="utf-8"))
        if current_manifest.exists()
        else None,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_admin(args: argparse.Namespace) -> int:
    from admin.server import run_admin_server

    print(f"Starting Glint Recognition Library Admin UI at http://{args.host}:{args.port}")
    run_admin_server(host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glint-lib", description="Glint cognitive supply chain CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch-l1", help="Fetch and snapshot L1 cognitive material sources")
    p_fetch.add_argument("--source", default="all")
    p_fetch.set_defaults(func=cmd_fetch_l1)

    p_l0 = sub.add_parser("ingest-l0", help="Refine L0 raw materials into L1 cognitive materials")
    p_l0.add_argument("--source", default="atlas_methods")
    p_l0.add_argument("--path", default=None)
    p_l0.add_argument("--limit", type=int, default=20)
    p_l0.add_argument("--offset", type=int, default=0)
    p_l0.add_argument("--model", default=DEFAULT_MODEL)
    p_l0.add_argument("--workers", type=int, default=2)
    p_l0.add_argument("--force", action="store_true")
    p_l0.set_defaults(func=cmd_ingest_l0)

    p_l2 = sub.add_parser("compile-l2", help="Compile L1 materials into validated L2 Glint lenses")
    p_l2.add_argument("--source", default="all")
    p_l2.add_argument("--limit", type=int, default=None)
    p_l2.add_argument("--model", default=DEFAULT_MODEL)
    p_l2.add_argument("--workers", type=int, default=4)
    p_l2.add_argument("--no-dedup", action="store_true")
    p_l2.add_argument("--force", action="store_true")
    p_l2.set_defaults(func=cmd_compile_l2)

    p_val = sub.add_parser("validate", help="Validate all L2 lenses and overlays against ontology.yaml")
    p_val.set_defaults(func=cmd_validate)

    p_clean = sub.add_parser("clean-l3", help="Run deterministic L2 -> L3 cleaning, normalization, and dedup")
    p_clean.add_argument("--no-dedup", action="store_true")
    p_clean.set_defaults(func=cmd_clean_l3)

    p_build = sub.add_parser("build", help="Clean L2->L3, embed routing profiles, and atomically publish dist/current")
    p_build.add_argument("--no-embed", action="store_true")
    p_build.set_defaults(func=cmd_build)

    p_rb = sub.add_parser("rollback", help="Roll back dist/current symlink to previous release")
    p_rb.add_argument("--version", default=None)
    p_rb.set_defaults(func=cmd_rollback)

    p_st = sub.add_parser("status", help="Show supply chain status")
    p_st.set_defaults(func=cmd_status)

    p_admin = sub.add_parser("admin", help="Launch local Cognitive Library Admin UI")
    p_admin.add_argument("--host", default="127.0.0.1")
    p_admin.add_argument("--port", type=int, default=18770)
    p_admin.set_defaults(func=cmd_admin)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
