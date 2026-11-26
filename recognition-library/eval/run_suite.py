"""Comprehensive multi-dataset Glint routing benchmark + Stage 1/2 recall diagnostics."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
import yaml

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_SUITE = [
    ("golden", EVAL_DIR / "golden_cases.yaml"),
    ("everyday", EVAL_DIR / "datasets" / "everyday_simulated.yaml"),
    ("user_history", EVAL_DIR / "datasets" / "user_history_real.yaml"),
    ("vague_stress", EVAL_DIR / "datasets" / "vague_ambiguous_stress.yaml"),
]


def _compute_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    n = max(1, len(records))
    lats = sorted(r["latency_ms"] for r in records)
    p50 = statistics.median(lats) if lats else 0.0
    p95 = lats[min(len(lats) - 1, int(len(lats) * 0.95))] if lats else 0.0
    top1_scores = [r["top1_score"] for r in records if r["top1_score"] is not None]
    return {
        "total": len(records),
        "hit_at_1": round(sum(1 for r in records if r["is_hit1"]) / n, 4),
        "hit_at_3": round(sum(1 for r in records if r["is_hit3"]) / n, 4),
        "prim_at_1": round(sum(1 for r in records if r["is_prim1"]) / n, 4),
        "forbidden_rate": round(sum(1 for r in records if r["has_forbidden"]) / n, 4),
        "strict_pass_rate": round(sum(1 for r in records if r["is_hit3"] and not r["has_forbidden"]) / n, 4),
        "orthogonality_rate": round(sum(1 for r in records if r["is_ortho"]) / n, 4),
        "l3_breakthrough_count": sum(1 for r in records if r["has_l3_breakthrough"]),
        "miss_count": sum(1 for r in records if not r["is_hit3"] or r["has_forbidden"]),
        "p50_ms": round(p50, 1),
        "p95_ms": round(p95, 1),
        "avg_top1_score": round(statistics.mean(top1_scores), 3) if top1_scores else None,
    }


def run_suite(
    *,
    base_url: str = "http://127.0.0.1:18765",
    datasets: list[tuple[str, Path]] = DEFAULT_SUITE,
    inspect_misses: bool = True,
    out_path: Path | None = None,
) -> dict[str, Any]:
    all_records: list[dict[str, Any]] = []
    by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)

    with httpx.Client(base_url=base_url.rstrip("/"), timeout=30.0) as client:
        health = client.get("/health").json()
        print(
            f"Target: {base_url} | mode={health.get('router_mode')} | "
            f"cards={health.get('cards_loaded')} | release={health.get('release_version')}"
        )
        client.post("/warmup")

        for ds_name, ds_path in datasets:
            if not ds_path.exists():
                print(f"[SKIP] Missing dataset: {ds_path}")
                continue
            raw = yaml.safe_load(ds_path.read_text(encoding="utf-8")) or {}
            cases: list[dict[str, Any]] = raw.get("cases", [])
            print(f"\nRunning dataset '{ds_name}' ({len(cases)} cases)...")

            for case in cases:
                resp = client.post(
                    "/route",
                    json={"app_name": case["app_name"], "input_text": case["input_text"]},
                )
                resp.raise_for_status()
                body = resp.json()
                cards = body.get("cards", [])
                lat = float(body.get("latency_ms", 0.0))

                got_ids = [c["id"] for c in cards]
                got_prims = [c.get("primitive") or c.get("dimension") for c in cards]
                exp_ids = set(case.get("expected_ids") or [])
                exp_prims = set(case.get("expected_primitives") or [])
                forb_ids = set(case.get("forbidden_ids") or [])

                is_hit1 = bool(got_ids and got_ids[0] in exp_ids)
                is_hit3 = any(cid in exp_ids for cid in got_ids)
                is_prim1 = bool(got_prims and got_prims[0] in exp_prims)
                forb_triggered = [cid for cid in got_ids if cid in forb_ids]
                has_forb = bool(forb_triggered)
                is_ortho = len(set(got_prims)) == len(got_prims)
                has_bt = any(bool(c.get("is_l3_breakthrough")) for c in cards)
                top1_score = cards[0].get("score") if cards else None

                bucket = str(case.get("bucket") or "golden")
                rec: dict[str, Any] = {
                    "dataset": ds_name,
                    "bucket": bucket,
                    "difficulty": case.get("difficulty", "standard"),
                    "id": case["id"],
                    "app_name": case["app_name"],
                    "input_text": case["input_text"],
                    "notes": case.get("notes", ""),
                    "expected_primitives": sorted(exp_prims),
                    "expected_ids": sorted(exp_ids),
                    "forbidden_ids": sorted(forb_ids),
                    "got_cards": [
                        {
                            "id": c["id"],
                            "title": c["title"],
                            "primitive": c.get("primitive") or c.get("dimension"),
                            "source": c.get("source"),
                            "score": c.get("score"),
                            "is_l3_breakthrough": bool(c.get("is_l3_breakthrough")),
                        }
                        for c in cards
                    ],
                    "intent": body.get("intent"),
                    "used_fallback": bool(body.get("used_fallback")),
                    "latency_ms": lat,
                    "top1_score": top1_score,
                    "is_hit1": is_hit1,
                    "is_hit3": is_hit3,
                    "is_prim1": is_prim1,
                    "has_forbidden": has_forb,
                    "forbidden_triggered": forb_triggered,
                    "is_ortho": is_ortho,
                    "has_l3_breakthrough": has_bt,
                    "strict_pass": is_hit3 and not has_forb,
                }

                # Diagnose MISS or FORBIDDEN (or Hit@1 miss on hard/vague) via inspect-recall
                if inspect_misses and (not is_hit3 or has_forb):
                    try:
                        ir = client.post(
                            "/admin/api/inspect-recall",
                            json={"query": case["input_text"], "app_name": case["app_name"], "top_k": 20},
                        )
                        if ir.status_code == 200:
                            idata = ir.json()
                            ranked = idata.get("ranked_all", [])
                            exp_in_pool = [
                                {
                                    "rank": r.get("rank"),
                                    "id": r["id"],
                                    "title": r["title"],
                                    "shelf": r["shelf"],
                                    "cosine": r["cosine"],
                                    "bm25": r["bm25"],
                                    "rerank_logit": r["rerank_logit"],
                                    "final_score": r["final_score"],
                                    "breakthrough_status": r.get("breakthrough_status"),
                                }
                                for r in ranked
                                if r["id"] in exp_ids
                            ]
                            rec["diagnostic"] = {
                                "cleaned_query": idata.get("cleaned_query"),
                                "expected_found_in_top20": exp_in_pool,
                                "top5_ranked": [
                                    {
                                        "rank": r.get("rank"),
                                        "id": r["id"],
                                        "title": r["title"],
                                        "primitive": r["primitive"],
                                        "shelf": r["shelf"],
                                        "cosine": r["cosine"],
                                        "bm25": r["bm25"],
                                        "rerank_logit": r["rerank_logit"],
                                        "final_score": r["final_score"],
                                    }
                                    for r in ranked[:5]
                                ],
                            }
                    except Exception as exc:
                        rec["diagnostic_error"] = str(exc)

                all_records.append(rec)
                by_dataset[ds_name].append(rec)
                by_bucket[f"{ds_name}/{bucket}"].append(rec)

    new_records = [r for r in all_records if r["dataset"] != "golden"]
    summary = {
        "health": health,
        "overall": _compute_metrics(all_records),
        "new_suite_overall": _compute_metrics(new_records),
        "by_dataset": {k: _compute_metrics(v) for k, v in by_dataset.items()},
        "by_bucket": {k: _compute_metrics(v) for k, v in by_bucket.items()},
        "records": all_records,
    }

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n================ SUITE SUMMARY ================")
    header = f"{'Dataset':16s} | {'N':>4s} | {'Hit@1':>6s} | {'Hit@3':>6s} | {'Prim@1':>6s} | {'Forb':>6s} | {'Pass':>6s} | {'P50ms':>6s} | {'AvgS1':>6s}"
    print(header)
    print("-" * len(header))
    for ds_name, m in summary["by_dataset"].items():
        print(
            f"{ds_name:16s} | {m['total']:4d} | {m['hit_at_1']*100:5.1f}% | {m['hit_at_3']*100:5.1f}% | "
            f"{m['prim_at_1']*100:5.1f}% | {m['forbidden_rate']*100:5.1f}% | {m['strict_pass_rate']*100:5.1f}% | "
            f"{m['p50_ms']:6.1f} | {str(m['avg_top1_score']):>6s}"
        )
    print("-" * len(header))
    for label, m in [
        (f"NEW {len(new_records)} TOTAL", summary["new_suite_overall"]),
        (f"ALL {len(all_records)} TOTAL", summary["overall"]),
    ]:
        print(
            f"{label:16s} | {m['total']:4d} | {m['hit_at_1']*100:5.1f}% | {m['hit_at_3']*100:5.1f}% | "
            f"{m['prim_at_1']*100:5.1f}% | {m['forbidden_rate']*100:5.1f}% | {m['strict_pass_rate']*100:5.1f}% | "
            f"{m['p50_ms']:6.1f} | {str(m['avg_top1_score']):>6s}"
        )

    print("\n================ BY BUCKET ================")
    for bkey, m in summary["by_bucket"].items():
        print(
            f"  {bkey:34s} (n={m['total']:2d}) | Hit@1={m['hit_at_1']*100:5.1f}% | "
            f"Hit@3={m['hit_at_3']*100:5.1f}% | Prim@1={m['prim_at_1']*100:5.1f}% | "
            f"Forb={m['forbidden_rate']*100:4.1f}% | Miss={m['miss_count']:2d}"
        )

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run comprehensive Glint routing benchmark suite")
    parser.add_argument("--url", default="http://127.0.0.1:18765")
    parser.add_argument("--out", default="")
    parser.add_argument("--no-inspect", action="store_true")
    args = parser.parse_args()
    out = Path(args.out) if args.out else None
    run_suite(base_url=args.url, inspect_misses=not args.no_inspect, out_path=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
