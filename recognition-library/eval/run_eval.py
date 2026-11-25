"""Golden Eval runner for Glint brain-server routing quality & latency gate."""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_CASES_PATH = EVAL_DIR / "golden_cases.yaml"


def run_eval(
    *,
    base_url: str = "http://127.0.0.1:18765",
    cases_path: Path = DEFAULT_CASES_PATH,
    verbose: bool = False,
) -> dict[str, Any]:
    data = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or {}
    cases: list[dict[str, Any]] = data.get("cases", [])

    hit1_id = 0
    hit3_id = 0
    hit1_prim = 0
    forbidden_hits = 0
    ortho_ok = 0
    latencies: list[float] = []
    bad_cases: list[dict[str, Any]] = []

    with httpx.Client(base_url=base_url.rstrip("/"), timeout=15.0) as client:
        health = client.get("/health").json()
        print(
            f"Target: {base_url} | mode={health.get('router_mode')} | "
            f"cards={health.get('cards_loaded')} | release={health.get('release_version')}"
        )
        # Warmup once
        client.post("/warmup")

        for case in cases:
            resp = client.post(
                "/route",
                json={"app_name": case["app_name"], "input_text": case["input_text"]},
            )
            resp.raise_for_status()
            body = resp.json()
            cards = body.get("cards", [])
            lat = float(body.get("latency_ms", 0.0))
            latencies.append(lat)

            got_ids = [c["id"] for c in cards]
            got_prims = [c.get("primitive") or c.get("dimension") for c in cards]
            exp_ids = set(case.get("expected_ids") or [])
            exp_prims = set(case.get("expected_primitives") or [])
            forb_ids = set(case.get("forbidden_ids") or [])

            is_hit1 = bool(got_ids and got_ids[0] in exp_ids)
            is_hit3 = any(cid in exp_ids for cid in got_ids)
            is_prim1 = bool(got_prims and got_prims[0] in exp_prims)
            has_forb = any(cid in forb_ids for cid in got_ids)
            is_ortho = len(set(got_prims)) == len(got_prims)

            if is_hit1:
                hit1_id += 1
            if is_hit3:
                hit3_id += 1
            if is_prim1:
                hit1_prim += 1
            if has_forb:
                forbidden_hits += 1
            if is_ortho:
                ortho_ok += 1

            status_tag = "OK" if (is_hit3 and not has_forb) else "MISS"
            if status_tag == "MISS":
                bad_cases.append(
                    {
                        "id": case["id"],
                        "input": case["input_text"],
                        "got": [(c["id"], c["title"], c.get("primitive")) for c in cards],
                        "expected_ids": sorted(exp_ids),
                        "forbidden_triggered": [cid for cid in got_ids if cid in forb_ids],
                    }
                )
            if verbose or status_tag == "MISS":
                titles = " | ".join(f"[{c.get('primitive')}]{c['title']}" for c in cards)
                print(f"  [{status_tag:4s}] {case['id']:28s} ({lat:5.0f}ms) -> {titles}")

    n = max(1, len(cases))
    sorted_lat = sorted(latencies)
    p50 = statistics.median(sorted_lat) if sorted_lat else 0.0
    p95 = sorted_lat[min(len(sorted_lat) - 1, int(len(sorted_lat) * 0.95))] if sorted_lat else 0.0

    summary = {
        "total_cases": len(cases),
        "hit_at_1": round(hit1_id / n, 4),
        "hit_at_3": round(hit3_id / n, 4),
        "prim_at_1": round(hit1_prim / n, 4),
        "forbidden_rate": round(forbidden_hits / n, 4),
        "orthogonality_rate": round(ortho_ok / n, 4),
        "p50_ms": round(p50, 1),
        "p95_ms": round(p95, 1),
        "bad_cases_count": len(bad_cases),
    }
    print("\n=== GOLDEN EVAL SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k:20s}: {v}")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Glint Golden Eval benchmark")
    parser.add_argument("--url", default="http://127.0.0.1:18765")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    run_eval(base_url=args.url, cases_path=Path(args.cases), verbose=args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
