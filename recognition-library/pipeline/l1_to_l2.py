"""Stage 2 Pipeline: L1 认知素材 -> L2 Glint 认知素材 (Gate 2 Normalization + Gate 3 Semantic Dedup)."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import yaml

from pipeline.embed_client import Embedder
from pipeline.ontology import (
    LIB_ROOT,
    Ontology,
    extract_placeholders,
    load_policy,
    validate_placeholders,
)
from pipeline.opencode_client import (
    ContentBlockedOpenCodeError,
    FatalOpenCodeError,
    OpenCodeResponse,
)
from pipeline.schema import (
    L1Material,
    L2Lens,
    LensExecution,
    LensMeta,
    LensRouting,
    QuarantineRecord,
)

logger = logging.getLogger("glint.pipeline.l1_to_l2")

DEFAULT_L2_PROMPT_PATH = LIB_ROOT / "prompts" / "l1_to_l2_enrich.md"


class JsonLLM(Protocol):
    model: str

    def call_json(self, prompt: str) -> OpenCodeResponse:
        ...


@dataclass
class L2CompileSummary:
    processed: int = 0
    compiled: int = 0
    quarantined: int = 0
    skipped_cached: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def load_existing_l2_lenses(l2_dir: Path) -> dict[str, L2Lens]:
    """Load all existing L2 lenses from data/l2_lenses/<primitive>/<id>.yaml."""
    lenses: dict[str, L2Lens] = {}
    if not l2_dir.exists():
        return lenses
    for path in sorted(l2_dir.rglob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            continue
        lens = L2Lens.model_validate(raw)
        lenses[lens.id] = lens
    return lenses


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )


def normalize_and_validate_draft(
    *,
    material: L1Material,
    draft: dict[str, Any],
    ontology: Ontology,
    min_confidence: float = 0.60,
    min_triggers: int = 2,
) -> tuple[L2Lens | None, list[str]]:
    """Gate 2: Deterministic normalization & strict ontology validation.

    Returns (valid_lens, reasons). If reasons is non-empty, lens is None and must be quarantined.
    """
    reasons: list[str] = []

    confidence = float(draft.get("confidence", 0.0) or 0.0)
    if confidence < min_confidence:
        reasons.append(f"low_confidence:{confidence:.2f}<{min_confidence:.2f}")

    title = str(draft.get("title") or material.title).strip()
    tagline = str(draft.get("tagline") or material.problem_solved or title).strip()
    if len(title) < 2:
        reasons.append("empty_title")

    routing_raw = draft.get("routing") or {}
    exec_raw = draft.get("execution") or {}

    # Normalize closed-set enums via Ontology SSOT + Alias Map
    raw_primitive = str(routing_raw.get("primitive", "")).strip()
    primitive = ontology.normalize_primitive(raw_primitive)
    if primitive is None:
        reasons.append(f"unknown_primitive:{raw_primitive}")

    raw_goals = routing_raw.get("goals") or []
    if isinstance(raw_goals, str):
        raw_goals = [raw_goals]
    goals, unknown_goals = ontology.normalize_list("goals", [str(x) for x in raw_goals], ontology.goals)
    if unknown_goals:
        reasons.append(f"unknown_goals:{','.join(unknown_goals)}")
    if not goals:
        reasons.append("missing_goals")

    raw_clarities = routing_raw.get("clarity_fit") or []
    if isinstance(raw_clarities, str):
        raw_clarities = [raw_clarities]
    clarities, unknown_clarities = ontology.normalize_list(
        "clarities", [str(x) for x in raw_clarities], ontology.clarities
    )
    if unknown_clarities:
        reasons.append(f"unknown_clarity:{','.join(unknown_clarities)}")
    if not clarities:
        reasons.append("missing_clarity_fit")

    raw_action = str(exec_raw.get("action_type") or material.suggested_action_type or "inject_prompt").strip()
    action_type = ontology.normalize_action_type(raw_action)
    if action_type is None:
        reasons.append(f"unknown_action_type:{raw_action}")

    inject_mode_raw = exec_raw.get("inject_mode")
    inject_mode: str | None = None
    if action_type == "inject_prompt":
        cand_mode = str(inject_mode_raw or "append").strip().lower()
        if cand_mode not in ontology.inject_modes:
            reasons.append(f"unknown_inject_mode:{cand_mode}")
        else:
            inject_mode = cand_mode

    pain_point = str(routing_raw.get("pain_point") or "").strip()
    if len(pain_point) < 4:
        reasons.append("missing_pain_point")

    triggers = [str(x).strip() for x in (routing_raw.get("trigger_utterances") or []) if str(x).strip()]
    if len(triggers) < min_triggers:
        reasons.append(f"insufficient_trigger_utterances:{len(triggers)}<{min_triggers}")

    anti_triggers = [str(x).strip() for x in (routing_raw.get("anti_triggers") or []) if str(x).strip()]
    domains = [str(x).strip().lower() for x in (routing_raw.get("domains") or material.domain_hints) if str(x).strip()]
    app_affinity = [str(x).strip() for x in (routing_raw.get("app_affinity") or []) if str(x).strip()]

    content = str(exec_raw.get("content") or material.content).strip()
    if len(content) < 4:
        reasons.append("empty_execution_content")

    # Placeholder syntax & wrap-mode check
    if action_type is not None:
        reasons.extend(validate_placeholders(content, action_type, inject_mode))

    if reasons:
        return None, reasons

    # Auto-sync non-{{input}} placeholders into execution.variables
    detected_vars = [v for v in extract_placeholders(content) if v != "input"]
    declared_vars = [str(v).strip() for v in (exec_raw.get("variables") or []) if str(v).strip() and str(v).strip() != "input"]
    merged_vars = list(dict.fromkeys(declared_vars + detected_vars))

    lens = L2Lens(
        id=material.id,
        title=title,
        tagline=tagline,
        routing=LensRouting(
            primitive=primitive,  # type: ignore[arg-type]
            goals=goals,
            clarity_fit=clarities,
            domains=list(dict.fromkeys(domains)),
            app_affinity=list(dict.fromkeys(app_affinity)),
            pain_point=pain_point,
            trigger_utterances=triggers,
            anti_triggers=anti_triggers,
        ),
        execution=LensExecution(
            action_type=action_type,  # type: ignore[arg-type]
            inject_mode=inject_mode,  # type: ignore[arg-type]
            content=content,
            variables=merged_vars,
        ),
        meta=LensMeta(
            source=material.source,
            source_ref=material.source_ref,
            source_hash=material.content_hash,
            status="active",
            base_weight=1.0,
            confidence=confidence,
            version=1,
        ),
    )
    return lens, []


class SemanticDedupIndex:
    """In-memory cosine similarity index for Gate 3 cross-source deduplication."""

    def __init__(self, embedder: Embedder, threshold: float = 0.88) -> None:
        self._embedder = embedder
        self.threshold = threshold
        self._ids: list[str] = []
        self._matrix: np.ndarray | None = None

    def seed(self, lenses: list[L2Lens]) -> None:
        if not lenses:
            return
        texts = [lens.routing_profile_text() for lens in lenses]
        vecs = self._embedder.embed_texts(texts)
        self._ids = [lens.id for lens in lenses]
        self._matrix = vecs

    def check_and_add(self, lens: L2Lens) -> tuple[bool, str | None, float]:
        """Return (is_duplicate, conflict_id, max_similarity). Adds non-duplicates to index."""
        vec = self._embedder.embed_texts([lens.routing_profile_text()])
        if self._matrix is not None and len(self._ids) > 0:
            sims = (self._matrix @ vec.T).reshape(-1)
            # Ignore self-comparison when recompiling the same ID
            best_idx = -1
            best_sim = -1.0
            for idx, existing_id in enumerate(self._ids):
                if existing_id == lens.id:
                    continue
                if float(sims[idx]) > best_sim:
                    best_sim = float(sims[idx])
                    best_idx = idx
            if best_idx >= 0 and best_sim >= self.threshold:
                return True, self._ids[best_idx], round(best_sim, 4)

        if self._matrix is None:
            self._matrix = vec
            self._ids = [lens.id]
        else:
            self._matrix = np.vstack([self._matrix, vec])
            self._ids.append(lens.id)
        return False, None, 0.0


def _build_l2_prompt(prompt_template: str, material: L1Material) -> str:
    extra_lines: list[str] = []
    if material.problem_solved:
        extra_lines.append(f"- 解决的痛点: {material.problem_solved}")
    if material.when_to_use:
        extra_lines.append(f"- 适用场景: {material.when_to_use}")
    if material.domain_hints:
        extra_lines.append(f"- 领域线索: {', '.join(material.domain_hints)}")
    if material.procedural_steps:
        extra_lines.append("- 程序性步骤:\n" + "\n".join(f"  * {s}" for s in material.procedural_steps[:6]))
    if material.anti_patterns:
        extra_lines.append("- 反模式/避坑:\n" + "\n".join(f"  * {a}" for a in material.anti_patterns[:4]))
    extra_block = "\n".join(extra_lines)

    return (
        f"{prompt_template.strip()}\n\n"
        f"---\n"
        f"## 待编译的 L1 认知素材\n"
        f"- ID: `{material.id}`\n"
        f"- 来源: `{material.source}` (`{material.source_ref}`)\n"
        f"- 建议动作类型: `{material.suggested_action_type}`\n"
        f"- 标题: {material.title}\n"
        f"{extra_block}\n\n"
        f"### 正文内容\n"
        f"{material.content[:5000]}\n"
    )


def compile_l1_to_l2(
    materials: list[L1Material],
    *,
    llm: JsonLLM,
    l2_dir: Path,
    quarantine_dir: Path,
    ontology: Ontology | None = None,
    embedder: Embedder | None = None,
    prompt_path: Path = DEFAULT_L2_PROMPT_PATH,
    workers: int = 1,
    force: bool = False,
) -> L2CompileSummary:
    """Compile a batch of L1 materials into validated, deduplicated L2 lenses."""
    ont = ontology or Ontology.load()
    policy = load_policy()
    gates_cfg = policy.get("gates", {})
    min_conf = float(gates_cfg.get("l2_min_confidence", 0.60))
    min_triggers = int(gates_cfg.get("min_trigger_utterances", 2))
    dedup_threshold = float(gates_cfg.get("semantic_dedup_threshold", 0.88))

    prompt_template = prompt_path.read_text(encoding="utf-8")
    existing = load_existing_l2_lenses(l2_dir)

    dedup_index: SemanticDedupIndex | None = None
    if embedder is not None:
        dedup_index = SemanticDedupIndex(embedder=embedder, threshold=dedup_threshold)
        dedup_index.seed(list(existing.values()))

    summary = L2CompileSummary()
    gate_lock = threading.Lock()

    to_process: list[L1Material] = []
    for mat in materials:
        summary.processed += 1
        if not force and mat.id in existing and existing[mat.id].meta.source_hash == mat.content_hash:
            summary.skipped_cached += 1
            continue
        q_path = quarantine_dir / f"{mat.id}.yaml"
        if not force and q_path.exists():
            try:
                q_data = yaml.safe_load(q_path.read_text(encoding="utf-8")) or {}
                if q_data.get("source_hash") == mat.content_hash:
                    summary.skipped_cached += 1
                    continue
            except Exception:
                pass
        to_process.append(mat)

    def _call_and_gate(mat: L1Material) -> tuple[str, str]:
        resp = llm.call_json(_build_l2_prompt(prompt_template, mat))
        draft = resp.parsed_json

        # Gate 2: Deterministic normalization & validation
        lens, reasons = normalize_and_validate_draft(
            material=mat,
            draft=draft,
            ontology=ont,
            min_confidence=min_conf,
            min_triggers=min_triggers,
        )
        if lens is None:
            record = QuarantineRecord(
                id=mat.id,
                source=mat.source,
                source_ref=mat.source_ref,
                source_hash=mat.content_hash,
                reasons=reasons,
                draft=draft,
            )
            with gate_lock:
                _write_yaml(quarantine_dir / f"{mat.id}.yaml", record.model_dump())
            return mat.id, "quarantined"

        # Gate 3 (locked for race-free dedup & write)
        with gate_lock:
            if dedup_index is not None:
                is_dup, dup_of, dup_score = dedup_index.check_and_add(lens)
                if is_dup:
                    record = QuarantineRecord(
                        id=mat.id,
                        source=mat.source,
                        source_ref=mat.source_ref,
                        source_hash=mat.content_hash,
                        reasons=[f"semantic_duplicate:{dup_of}({dup_score:.3f}>={dedup_threshold:.2f})"],
                        duplicate_of=dup_of,
                        duplicate_score=dup_score,
                        draft=lens.model_dump(),
                    )
                    _write_yaml(quarantine_dir / f"{mat.id}.yaml", record.model_dump())
                    return mat.id, "quarantined"

            if mat.id in existing and existing[mat.id].routing.primitive != lens.routing.primitive:
                old_path = l2_dir / existing[mat.id].routing.primitive / f"{mat.id}.yaml"
                old_path.unlink(missing_ok=True)

            target_path = l2_dir / lens.routing.primitive / f"{lens.id}.yaml"
            _write_yaml(target_path, lens.model_dump())
            existing[lens.id] = lens
            return mat.id, "compiled"

    if workers <= 1:
        for mat in to_process:
            try:
                _, status = _call_and_gate(mat)
                if status == "compiled":
                    summary.compiled += 1
                else:
                    summary.quarantined += 1
            except FatalOpenCodeError:
                raise
            except (ContentBlockedOpenCodeError, Exception) as exc:
                summary.failed += 1
                summary.errors.append(f"{mat.id}: {exc}")
                logger.warning("L1->L2 LLM call failed for %s: %s", mat.id, exc)
        return summary

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(_call_and_gate, mat): mat for mat in to_process}
        for future in as_completed(future_map):
            mat = future_map[future]
            try:
                _, status = future.result()
                if status == "compiled":
                    summary.compiled += 1
                else:
                    summary.quarantined += 1
            except FatalOpenCodeError:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            except (ContentBlockedOpenCodeError, Exception) as exc:
                summary.failed += 1
                summary.errors.append(f"{mat.id}: {exc}")
                logger.warning("L1->L2 LLM call failed for %s: %s", mat.id, exc)

    return summary
