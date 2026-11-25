"""Stage 1 Pipeline: L0 认知原料 -> L1 认知素材 (Gate 1 SNR Filter + Procedural Refiner)."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pipeline.ontology import LIB_ROOT, load_policy
from pipeline.opencode_client import (
    ContentBlockedOpenCodeError,
    FatalOpenCodeError,
    OpenCodeResponse,
)
from pipeline.schema import L0GateResult, L0RawItem, L1Material, content_sha256

logger = logging.getLogger("glint.pipeline.l0_to_l1")

DEFAULT_L0_PROMPT_PATH = LIB_ROOT / "prompts" / "l0_to_l1_refine.md"


class JsonLLM(Protocol):
    model: str

    def call_json(self, prompt: str) -> OpenCodeResponse:
        ...


@dataclass
class L0RefineSummary:
    processed: int = 0
    accepted: int = 0
    rejected: int = 0
    skipped_cached: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def _checkpoint_file(out_dir: Path, item_id: str) -> Path:
    return out_dir / f"{item_id}.json"


def _rejected_file(quarantine_dir: Path, item_id: str) -> Path:
    return quarantine_dir / "l0_rejected" / f"{item_id}.json"


def refine_single_l0(
    item: L0RawItem,
    *,
    llm: JsonLLM,
    prompt_template: str,
    out_dir: Path,
    quarantine_dir: Path,
    min_quality_score: float = 0.65,
    force: bool = False,
) -> tuple[str, Path | None]:
    """Refine one L0 item into L1Material or quarantine it.

    Returns (status, output_path) where status in {'skipped', 'accepted', 'rejected'}.
    """
    accepted_path = _checkpoint_file(out_dir, item.id)
    rejected_path = _rejected_file(quarantine_dir, item.id)

    if not force:
        if accepted_path.exists():
            existing = json.loads(accepted_path.read_text(encoding="utf-8"))
            if existing.get("metadata", {}).get("l0_content_hash") == item.content_hash:
                return "skipped", accepted_path
        if rejected_path.exists():
            existing = json.loads(rejected_path.read_text(encoding="utf-8"))
            if existing.get("content_hash") == item.content_hash:
                return "skipped", rejected_path

    full_prompt = (
        f"{prompt_template.strip()}\n\n"
        f"---\n"
        f"## 待筛查与精炼的 L0 认知原料\n"
        f"- ID: `{item.id}`\n"
        f"- 来源: `{item.source}` / `{item.source_ref}`\n\n"
        f"{item.raw_text}\n"
    )

    resp = llm.call_json(full_prompt)
    decision = L0GateResult.model_validate(resp.parsed_json)

    refined_text = (decision.refined_content or "").strip()

    # Gate 1: SNR & procedural knowledge check
    reject_reasons: list[str] = []
    if not decision.accept:
        reject_reasons.append(decision.reject_reason or "rejected_by_l0_gate")
    if decision.quality_score < min_quality_score:
        reject_reasons.append(f"quality_score_below_threshold:{decision.quality_score:.2f}<{min_quality_score:.2f}")
    if decision.accept and len(refined_text) < 20:
        reject_reasons.append("refined_content_too_short")

    if reject_reasons:
        rejected_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": item.id,
            "source": item.source,
            "source_ref": item.source_ref,
            "content_hash": item.content_hash,
            "title": item.title,
            "quality_score": decision.quality_score,
            "reasons": reject_reasons,
            "model": resp.model,
        }
        rejected_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return "rejected", rejected_path

    material = L1Material(
        id=item.id,
        source=item.source,
        source_ref=item.source_ref,
        title=(decision.title or "").strip() or item.title,
        content=refined_text,
        content_hash=content_sha256(refined_text),
        suggested_action_type="inject_prompt",
        domain_hints=decision.domain_hints or [],
        problem_solved=(decision.problem_solved or "").strip(),
        when_to_use=(decision.when_to_use or "").strip(),
        procedural_steps=decision.procedural_steps or [],
        anti_patterns=decision.anti_patterns or [],
        metadata={
            **item.metadata,
            "l0_content_hash": item.content_hash,
            "l0_quality_score": decision.quality_score,
            "refined_by": resp.model,
        },
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    accepted_path.write_text(material.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return "accepted", accepted_path


def run_l0_to_l1(
    items: list[L0RawItem],
    *,
    llm: JsonLLM,
    out_dir: Path,
    quarantine_dir: Path,
    prompt_path: Path = DEFAULT_L0_PROMPT_PATH,
    min_quality_score: float | None = None,
    workers: int = 2,
    force: bool = False,
) -> L0RefineSummary:
    """Batch-refine L0 raw items into L1 cognitive materials with checkpointing."""
    policy = load_policy()
    threshold = (
        min_quality_score
        if min_quality_score is not None
        else float(policy.get("gates", {}).get("l0_min_quality_score", 0.65))
    )
    prompt_template = prompt_path.read_text(encoding="utf-8")
    summary = L0RefineSummary()

    def _work(item: L0RawItem) -> tuple[str, str]:
        status, _ = refine_single_l0(
            item,
            llm=llm,
            prompt_template=prompt_template,
            out_dir=out_dir,
            quarantine_dir=quarantine_dir,
            min_quality_score=threshold,
            force=force,
        )
        return item.id, status

    if workers <= 1:
        for item in items:
            summary.processed += 1
            try:
                _, status = _work(item)
                if status == "accepted":
                    summary.accepted += 1
                elif status == "rejected":
                    summary.rejected += 1
                else:
                    summary.skipped_cached += 1
            except FatalOpenCodeError:
                raise
            except (ContentBlockedOpenCodeError, Exception) as exc:
                summary.failed += 1
                summary.errors.append(f"{item.id}: {exc}")
                logger.warning("L0->L1 failed for %s: %s", item.id, exc)
        return summary

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(_work, item): item for item in items}
        for future in as_completed(future_map):
            item = future_map[future]
            summary.processed += 1
            try:
                _, status = future.result()
                if status == "accepted":
                    summary.accepted += 1
                elif status == "rejected":
                    summary.rejected += 1
                else:
                    summary.skipped_cached += 1
            except FatalOpenCodeError:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            except (ContentBlockedOpenCodeError, Exception) as exc:
                summary.failed += 1
                summary.errors.append(f"{item.id}: {exc}")
                logger.warning("L0->L1 failed for %s: %s", item.id, exc)

    return summary
