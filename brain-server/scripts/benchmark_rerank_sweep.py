"""E3 Offline Reranker Latency vs. Accuracy Sweep (zero changes to live service).

Pre-embeds all eval queries once via Ollama (bge-m3), then sweeps:
  - candidate_pool_size (Stage 1 -> Stage 2 pool)
  - reranker max_length (Cross-Encoder token cap)
  - max_triggers in _rerank_doc_text (document compactness)

Reports rerank P50/P95 ms alongside Hit@1, Hit@3, Prim@1, Forbidden Rate, and Orthogonality
using the exact same scoring logic as recognition-library/eval/run_eval.py.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import yaml

BRAIN_DIR = Path(__file__).resolve().parents[1]
if str(BRAIN_DIR) not in sys.path:
    sys.path.insert(0, str(BRAIN_DIR))

from config import settings
from reranker import LocalBgeReranker
from router import OllamaClient
import skills.lens_router as lr_mod
from skills.lens_router import LensRouterSkill, _strip_style_modifiers, _vague_signal_score

DEFAULT_GOLDEN = BRAIN_DIR.parent / "recognition-library" / "eval" / "golden_cases.yaml"

SWEEP_CONFIGS: list[dict[str, Any]] = [
    {"name": "baseline (18 / 256 / 7t)", "pool": 18, "max_len": 256, "max_trigs": 7},
    {"name": "pool12   (12 / 256 / 7t)", "pool": 12, "max_len": 256, "max_trigs": 7},
    {"name": "pool10   (10 / 256 / 7t)", "pool": 10, "max_len": 256, "max_trigs": 7},
    {"name": "len160   (18 / 160 / 4t)", "pool": 18, "max_len": 160, "max_trigs": 4},
    {"name": "combo_A  (12 / 160 / 4t)", "pool": 12, "max_len": 160, "max_trigs": 4},
    {"name": "combo_B  (12 / 128 / 4t)", "pool": 12, "max_len": 128, "max_trigs": 4},
    {"name": "combo_C  (10 / 128 / 4t)", "pool": 10, "max_len": 128, "max_trigs": 4},
    {"name": "fast_D   ( 8 / 128 / 3t)", "pool": 8, "max_len": 128, "max_trigs": 3},
]


def _make_doc_fn(max_trigs: int):
    def _doc_text(lens: dict[str, Any]) -> str:
        routing = lens.get("routing", {})
        raw_trigs = [str(t) for t in (routing.get("trigger_utterances") or []) if str(t).strip()]
        if len(raw_trigs) <= max_trigs:
            selected = raw_trigs
        elif max_trigs <= 2:
            selected = raw_trigs[:max_trigs]
        else:
            head = min(2, max_trigs // 2)
            tail = max_trigs - head
            selected = list(dict.fromkeys(raw_trigs[:head] + raw_trigs[-tail:]))
        triggers = " / ".join(selected)
        parts = [
            f"{lens.get('title', '')}：{lens.get('tagline', '')}",
            f"痛点：{routing.get('pain_point', '')}",
        ]
        if triggers:
            parts.append(f"常见表达：{triggers}")
        return "；".join(parts)

    return _doc_text


async def eval_config(
    *,
    skill: LensRouterSkill,
    reranker: LocalBgeReranker,
    cases: list[dict[str, Any]],
    q_vecs: dict[str, Any],
    pool: int,
    max_len: int,
    max_trigs: int,
) -> dict[str, Any]:
    skill._candidate_pool_size = pool
    reranker._max_length = max_len
    doc_fn = _make_doc_fn(max_trigs)
    bt_cfg = skill._policy.get("l3_breakthrough") or {}

    hit1 = 0
    hit3 = 0
    prim1 = 0
    forb = 0
    ortho = 0
    rerank_ms_list: list[float] = []
    batch_sizes: list[int] = []

    # Warmup 2 calls for this max_len/pool shape on MPS
    for case in cases[:2]:
        trimmed = case["input_text"].strip()[: skill._max_input_chars]
        rtext = _strip_style_modifiers(trimmed)
        q_vec = q_vecs.get(case["id"])
        sim_map = {}
        if q_vec is not None and skill._embed_matrix is not None:
            sims = (skill._embed_matrix @ q_vec).reshape(-1)
            sim_map = {cid: float(sims[idx]) for idx, cid in enumerate(skill._embed_ids)}
        cands = skill._hybrid_recall_candidates(
            input_text=rtext,
            app_name=case["app_name"],
            sim_map=sim_map,
            vague_signal=_vague_signal_score(trimmed),
        )
        await reranker.score(rtext, [doc_fn(l) for _, l in cands])

    for case in cases:
        trimmed = case["input_text"].strip()[: skill._max_input_chars]
        rtext = _strip_style_modifiers(trimmed)
        vague_sig = _vague_signal_score(trimmed)
        q_vec = q_vecs.get(case["id"])
        sim_map: dict[str, float] = {}
        if q_vec is not None and skill._embed_matrix is not None:
            sims = (skill._embed_matrix @ q_vec).reshape(-1)
            sim_map = {cid: float(sims[idx]) for idx, cid in enumerate(skill._embed_ids)}

        candidates = skill._hybrid_recall_candidates(
            input_text=rtext,
            app_name=case["app_name"],
            sim_map=sim_map,
            vague_signal=vague_sig,
        )
        shadow_candidates = skill._shadow_recall_candidates(
            input_text=rtext,
            q_vec=q_vec,
            bt_cfg=bt_cfg,
        )
        all_docs = [doc_fn(l) for _, l in candidates] + [doc_fn(l) for _, l in shadow_candidates]
        batch_sizes.append(len(all_docs))

        t0 = time.perf_counter()
        scores = await reranker.score(rtext, all_docs)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        rerank_ms_list.append(dt_ms)

        assert scores is not None
        n_l4 = len(candidates)
        l4_scores = scores[:n_l4]
        sh_scores = scores[n_l4:]
        cards, l4_raw_ce, low_conf = skill._select_from_reranker(
            candidates=candidates,
            rerank_scores=l4_scores,
            app_name=case["app_name"],
            vague_signal=vague_sig,
        )
        if shadow_candidates and sh_scores:
            cards = skill._apply_l3_breakthrough(
                l4_cards=cards,
                l4_raw_ce_by_id=l4_raw_ce,
                shadow_candidates=shadow_candidates,
                shadow_scores=sh_scores,
                bt_cfg=bt_cfg,
                app_name=case["app_name"],
                vague_signal=vague_sig,
                low_conf_deficit=low_conf,
            )

        got_ids = [c.id for c in cards]
        got_prims = [c.primitive or c.dimension for c in cards]
        exp_ids = set(case.get("expected_ids") or [])
        exp_prims = set(case.get("expected_primitives") or [])
        forb_ids = set(case.get("forbidden_ids") or [])

        if got_ids and got_ids[0] in exp_ids:
            hit1 += 1
        if any(cid in exp_ids for cid in got_ids):
            hit3 += 1
        if got_prims and got_prims[0] in exp_prims:
            prim1 += 1
        if any(cid in forb_ids for cid in got_ids):
            forb += 1
        if len(set(got_prims)) == len(got_prims):
            ortho += 1

    n = max(1, len(cases))
    s_lat = sorted(rerank_ms_list)
    p50 = statistics.median(s_lat)
    p95 = s_lat[min(len(s_lat) - 1, int(len(s_lat) * 0.95))]
    return {
        "n": len(cases),
        "p50": p50,
        "p95": p95,
        "avg_batch": sum(batch_sizes) / len(batch_sizes),
        "hit1": hit1 / n * 100.0,
        "hit3": hit3 / n * 100.0,
        "prim1": prim1 / n * 100.0,
        "forb": forb / n * 100.0,
        "ortho": ortho / n * 100.0,
    }


async def main_async(case_paths: list[Path]) -> None:
    inference = OllamaClient(settings)
    reranker = LocalBgeReranker(model_name=settings.reranker_model)
    assert settings.dist_dir is not None
    skill = LensRouterSkill(
        dist_dir=settings.dist_dir,
        policy_path=settings.policy_path,
        inference=inference,
        reranker=reranker,
        top_k=settings.top_k_cards,
        max_input_chars=settings.max_input_chars,
    )

    print(f"Loaded {len(skill.raw_lenses)} L4 + {len(skill.raw_shadow_lenses)} L3 shadow | release={skill.release_version}")
    print("Warming up MPS reranker...")
    await reranker.warmup()

    datasets: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
    for path in case_paths:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cases = data.get("cases", [])
        print(f"Pre-embedding {len(cases)} cases from {path.name}...")
        q_vecs: dict[str, Any] = {}
        for case in cases:
            trimmed = case["input_text"].strip()[: skill._max_input_chars]
            rtext = _strip_style_modifiers(trimmed)
            q_vecs[case["id"]] = await skill._embed_query(rtext)
        datasets.append((path.stem, cases, q_vecs))

    for ds_name, cases, q_vecs in datasets:
        print(f"\n=== Dataset: {ds_name} ({len(cases)} cases) ===")
        header = f"{'Config':26s} | {'Batch':>5s} | {'P50ms':>6s} | {'P95ms':>6s} | {'Hit@1':>6s} | {'Hit@3':>6s} | {'Forb':>5s} | {'Ortho':>6s}"
        print(header)
        print("-" * len(header))
        for cfg in SWEEP_CONFIGS:
            res = await eval_config(
                skill=skill,
                reranker=reranker,
                cases=cases,
                q_vecs=q_vecs,
                pool=cfg["pool"],
                max_len=cfg["max_len"],
                max_trigs=cfg["max_trigs"],
            )
            print(
                f"{cfg['name']:26s} | {res['avg_batch']:5.1f} | {res['p50']:6.1f} | {res['p95']:6.1f} | "
                f"{res['hit1']:5.1f}% | {res['hit3']:5.1f}% | {res['forb']:4.1f}% | {res['ortho']:5.1f}%"
            )

    await inference.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline E3 reranker latency/accuracy sweep")
    parser.add_argument("--cases", nargs="+", default=[str(DEFAULT_GOLDEN)])
    args = parser.parse_args()
    asyncio.run(main_async([Path(p) for p in args.cases]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
