"""Phase 1 v2.2 Micro-Skill: Hybrid Recall (BM25 + bge-m3 RRF) -> Cross-Encoder Rerank (bge-reranker-v2-m3)."""

from __future__ import annotations

import json
import logging
import math
import random
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from models import CardItem, IntentPrediction, RouteRequest, RouteResponse
from reranker import RerankerClient
from router import InferenceClient
from skills import BaseSkill

logger = logging.getLogger("glint.skills.lens_router")

PRIMITIVES = ("compress", "deconstruct", "critique", "diverge", "converge", "translate", "metacognize")
GOALS = ("create", "optimize", "debug", "evaluate", "digest")
CLARITIES = ("vague", "concrete", "conflicted")

RERANK_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 3,
            "maxItems": 3,
        },
        "primitive": {"type": "string", "enum": list(PRIMITIVES)},
        "goal": {"type": "string", "enum": list(GOALS)},
        "clarity": {"type": "string", "enum": list(CLARITIES)},
    },
    "required": ["picks", "primitive", "goal", "clarity"],
}

SYSTEM_PROMPT_V2 = """你是Glint认知透镜精排路由器。根据App和用户输入，从下方候选中选出最贴合用户当下真实意图的3个卡片编号（按优先级排序，尽量选不同元操作，坚决排除无关领域），并给出主意图。
规则：风格/格式要求(如“去AI味、只给表格”)不是主意图，不干扰判断；仅当润色/改写是唯一动作时才选translate。
仅输出JSON: {"picks":[编号1,编号2,编号3],"primitive":"...","goal":"...","clarity":"..."}"""

ZERO_FRICTION_VAR_NOTE = (
    "\n\n（若上述框架所需的背景变量在上下文中未明确给出，请先根据上下文合理推断，或先简短反问 1–2 个关键问题再给完整输出。）"
)

TOKEN_RE = re.compile(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]+")
MODIFIER_CLAUSE_RE = re.compile(
    r"(?:^|[，,、。\s；;])(?:硬核一点|通俗一点|简洁一点|专业一点|别废话|不要废话|去\s*ai\s*味|去掉\s*ai\s*味|"
    r"只给表格|给表格|用大白话|直接给对比表|用表格输出|markdown\s*表格|简短一点|直接给结论)(?=[，,、。\s；;：:]|$)",
    re.IGNORECASE,
)

# High-frequency Chinese conversational filler bigrams that dilute BM25 precision
ZH_STOP_BIGRAMS = frozenset(
    {
        "帮我",
        "一下",
        "这个",
        "那个",
        "什么",
        "可以",
        "有没有",
        "能不能",
        "应该",
        "一些",
        "一个",
        "几个",
        "关于",
        "进行",
        "需要",
        "想要",
        "觉得",
        "感觉",
        "东西",
        "地方",
        "知道",
        "告诉",
        "怎么",
        "如何",
        "为什么",
        "其实",
        "主要",
        "但是",
        "最近",
        "现在",
    }
)

DEV_APPS = ("cursor", "vscode", "code", "xcode", "terminal", "iterm", "warp", "ghostty", "codex", "claude code", "opencode", "pycharm")
COMMS_APPS = ("feishu", "slack", "mail", "wechat", "discord", "teams", "outlook")
DESIGN_APPS = ("figma", "sketch", "framer")
SHEET_APPS = ("excel", "numbers", "sheets")

# Core universal metacognitive / clarification safety-net anchors injected into Stage 2 on vague queries
VAGUE_ANCHOR_IDS: tuple[str, ...] = (
    "core_clarify_first",
    "cle_m2_c_c1_specification",
    "cle_m2_c_p2_explication",
    "cle_m2_c_c4_problem_formation",
)

VAGUE_EXPLICIT_RE = re.compile(
    r"(?:不知道(?:从哪|怎么|做啥|写啥|选哪|该不该|要什么|做什么|写什么|去哪)|"
    r"没方向|没头绪|乱糟糟|说不上来|说不出|讲不明白|太笼统|太抽象|"
    r"不够.{1,3}|怎么.{0,4}(?:更好|更有)|提不起劲|内耗|迷茫|哪儿怪怪|不对劲|"
    r"理理头绪|怎么提升|怎么才能快速|靠不靠谱|不太一样|不在一个频道)",
    re.IGNORECASE,
)
VAGUE_DEICTIC_RE = re.compile(
    r"^(?:帮我|请)?(?:看看|改改|分析一下|总结一下|写个方案|给点建议|给我点建议|这个怎么用|这个合理吗|接下来怎么办|有点不对劲|帮我看看这个|这个是什么)[？?。！!….\s]*$"
)
HALF_TYPED_TAIL_RE = re.compile(r"(?:\.{2,}|…+|感觉|主要是|但是大家|不过|其实|总感觉|总觉得)[，,、\s]*$")
WORKFLOW_CONTINUE_RE = re.compile(
    r"(?:继续|下一步|下步|接着|接下来|然后呢|再来|按这个|展开说说|next|continue)",
    re.IGNORECASE,
)


def _is_workflow_continuation(text: str) -> bool:
    """Detect short or explicit follow-up cues that legitimately continue an active workflow."""
    t = text.strip()
    if not t:
        return False
    if WORKFLOW_CONTINUE_RE.search(t) and len(t) <= 32:
        return True
    return len(t) <= 6


def _vague_signal_score(text: str) -> float:
    """Estimate linguistic vagueness / underspecification in [0.0, 1.0]."""
    t = text.strip()
    if not t:
        return 0.0
    score = 0.0
    if VAGUE_DEICTIC_RE.match(t):
        score = max(score, 0.95)
    if HALF_TYPED_TAIL_RE.search(t):
        score = max(score, 0.75)
    if VAGUE_EXPLICIT_RE.search(t):
        score = max(score, 0.80)
    # Ultra-short phrase without explicit punctuation/context
    if len(t) <= 6:
        score = max(score, 0.50)
    return min(1.0, score)


def _app_category_bonus(app_lower: str, category: str, domains: list[str]) -> float:
    """Gentle domain prior based on active application (never hard-filters)."""
    if not app_lower:
        return 0.0
    if any(k in app_lower for k in DEV_APPS):
        if category == "engineering":
            return 0.28
    elif any(k in app_lower for k in COMMS_APPS):
        if category == "writing_comms":
            return 0.15
    elif any(k in app_lower for k in DESIGN_APPS):
        if category == "product_business" or any(d in ("design", "ui", "ux", "visual") for d in domains):
            return 0.20
    elif any(k in app_lower for k in SHEET_APPS):
        if any(d in ("spreadsheet", "excel", "data", "analytics", "finance") for d in domains):
            return 0.25
    return 0.0


def _strip_style_modifiers(text: str) -> str:
    """Enforce '要求 != 意图' before retrieval when a substantive core intent remains."""
    cleaned = MODIFIER_CLAUSE_RE.sub(" ", text).strip(" ，,、。；;：:")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if len(cleaned) >= 6 else text


def _tokenize_zh_en(text: str, *, drop_stopwords: bool = True) -> list[str]:
    """Lightweight Chinese bigram + ASCII word tokenizer for zero-dependency BM25."""
    raw_tokens: list[str] = []
    for seg in TOKEN_RE.findall((text or "").lower()):
        if seg.isascii():
            raw_tokens.append(seg)
        else:
            if len(seg) == 1:
                raw_tokens.append(seg)
            else:
                for i in range(len(seg) - 1):
                    raw_tokens.append(seg[i : i + 2])
    if not drop_stopwords or not raw_tokens:
        return raw_tokens
    filtered = [tok for tok in raw_tokens if tok not in ZH_STOP_BIGRAMS]
    return filtered if filtered else raw_tokens


def _compact_preview(text: str, max_chars: int = 44) -> str:
    compact = re.sub(r"\s+", " ", text.replace("{{input}}", "").strip())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"


def _rerank_doc_text(lens: dict[str, Any], query: str = "") -> str:
    """Semantic profile fed to Cross-Encoder bge-reranker-v2-m3 (never drops user triggers)."""
    routing = lens.get("routing", {})
    raw_trigs = list(dict.fromkeys(str(t).strip() for t in (routing.get("trigger_utterances") or []) if str(t).strip()))
    if query and len(raw_trigs) > 1:
        q_toks = set(_tokenize_zh_en(query, drop_stopwords=False))
        if q_toks:
            raw_trigs.sort(
                key=lambda t: len(q_toks & set(_tokenize_zh_en(t, drop_stopwords=False))),
                reverse=True,
            )
    triggers = " / ".join(raw_trigs)
    parts = [
        f"{lens.get('title', '')}：{lens.get('tagline', '')}",
        f"痛点：{routing.get('pain_point', '')}",
    ]
    if triggers:
        parts.append(f"常见表达：{triggers}")
    return "；".join(parts)


class _LightweightBM25:
    """Pre-indexed in-memory BM25 over L4 lenses (< 0.5 ms per query)."""

    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        self._n = len(docs_tokens)
        self._doc_lens = [len(toks) for toks in docs_tokens]
        self._avgdl = (sum(self._doc_lens) / self._n) if self._n else 1.0
        self._tfs = [Counter(toks) for toks in docs_tokens]
        df: Counter[str] = Counter()
        for tf in self._tfs:
            for term in tf:
                df[term] += 1
        self._idf = {
            term: math.log(1.0 + (self._n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def score_all(self, query_tokens: list[str]) -> list[float]:
        if not query_tokens or self._n == 0:
            return [0.0] * self._n
        q_unique = set(query_tokens)
        scores: list[float] = []
        for idx, tf in enumerate(self._tfs):
            dl = self._doc_lens[idx]
            denom_base = self._k1 * (1.0 - self._b + self._b * (dl / self._avgdl))
            s = 0.0
            for term in q_unique:
                f = tf.get(term, 0)
                if f > 0:
                    idf = self._idf.get(term, 0.0)
                    s += idf * (f * (self._k1 + 1.0)) / (f + denom_base)
            scores.append(s)
        return scores


class LensRouterSkill(BaseSkill):
    """Two-stage v2.2 router: Stage 1 BM25+bge-m3 Hybrid RRF -> Stage 2 bge-reranker-v2-m3 (or 2B fallback)."""

    name = "cognitive_cards"

    def __init__(
        self,
        *,
        dist_dir: Path,
        policy_path: Path,
        inference: InferenceClient,
        reranker: RerankerClient | None = None,
        top_k: int = 3,
        candidate_pool_size: int = 18,
        max_input_chars: int = 1200,
        rng: random.Random | None = None,
    ) -> None:
        self._dist_dir = dist_dir
        self._policy_path = policy_path
        self._inference = inference
        self._reranker = reranker
        self._top_k = top_k
        self._candidate_pool_size = candidate_pool_size
        self._max_input_chars = max_input_chars
        self._rng = rng or random.Random()

        self._release_key: tuple[tuple[str, int], ...] = ()
        self._release_version: str | None = None
        self._lenses: list[dict[str, Any]] = []
        self._lenses_by_id: dict[str, dict[str, Any]] = {}
        self._lenses_by_primitive: dict[str, list[dict[str, Any]]] = {}
        self._embed_ids: list[str] = []
        self._embed_idx_by_id: dict[str, int] = {}
        self._embed_matrix: np.ndarray | None = None
        self._bm25: _LightweightBM25 | None = None
        self._shadow_lenses: list[dict[str, Any]] = []
        self._shadow_embed_ids: list[str] = []
        self._shadow_embed_matrix: np.ndarray | None = None
        self._shadow_bm25: _LightweightBM25 | None = None
        self._policy: dict[str, Any] = {}
        self._state_lock = threading.RLock()
        self._workflow_sessions: dict[str, tuple[str, float]] = {}
        self._workflow_ttl_s: float = 600.0

        self.reload_if_changed()

    def _stage2_params(self) -> dict[str, float]:
        s2 = self._policy.get("stage2_rerank") or {}
        wf = self._policy.get("workflow") or {}
        return {
            "evergreen_boost": float(s2.get("evergreen_boost", 0.45)),
            "app_affinity_boost": float(s2.get("app_affinity_boost", 0.20)),
            "base_weight_scale": float(s2.get("base_weight_scale", 0.55)),
            "cliff_score_floor": float(s2.get("cliff_score_floor", -4.2)),
            "cliff_margin_below_top1": float(s2.get("cliff_margin_below_top1", 3.5)),
            "workflow_ttl_seconds": float(wf.get("ttl_seconds", self._workflow_ttl_s)),
            "workflow_min_rerank_logit": float(wf.get("min_rerank_logit", -1.2)),
            "workflow_min_cosine": float(wf.get("min_cosine", 0.45)),
        }

    def record_selection(self, app_name: str, selected_id: str | None) -> None:
        """Track lightweight per-app workflow state for next-step chaining."""
        key = app_name.strip().lower() or "unknown"
        if not selected_id or selected_id == "__custom__":
            return
        with self._state_lock:
            lens = self._lenses_by_id.get(selected_id)
            if not lens:
                self._workflow_sessions.pop(key, None)
                return
            wf = lens.get("execution", {}).get("workflow") or {}
            next_id = wf.get("next_lens_id")
            ttl = self._stage2_params()["workflow_ttl_seconds"]
            if next_id and next_id in self._lenses_by_id:
                self._workflow_sessions[key] = (str(next_id), time.monotonic() + ttl)
                logger.info("Workflow session [%s]: next step pinned -> %s", app_name, next_id)
            else:
                self._workflow_sessions.pop(key, None)

    def _get_pinned_workflow_lens(self, app_name: str) -> dict[str, Any] | None:
        key = app_name.strip().lower() or "unknown"
        with self._state_lock:
            entry = self._workflow_sessions.get(key)
            if not entry:
                return None
            next_id, expires_at = entry
            if time.monotonic() > expires_at:
                self._workflow_sessions.pop(key, None)
                return None
            return self._lenses_by_id.get(next_id)

    def _clear_workflow_session(self, app_name: str) -> None:
        key = app_name.strip().lower() or "unknown"
        with self._state_lock:
            self._workflow_sessions.pop(key, None)

    @property
    def system_prompt(self) -> str:
        return SYSTEM_PROMPT_V2

    @property
    def release_version(self) -> str | None:
        self.reload_if_changed()
        return self._release_version

    @property
    def cards(self) -> list[CardItem]:
        self.reload_if_changed()
        return [self._to_card_item(lens) for lens in self._lenses]

    @property
    def raw_lenses(self) -> list[dict[str, Any]]:
        self.reload_if_changed()
        return list(self._lenses)

    @property
    def raw_shadow_lenses(self) -> list[dict[str, Any]]:
        self.reload_if_changed()
        return list(self._shadow_lenses)

    def reload_if_changed(self) -> bool:
        """Hot-reload L4 dist/current (including symlink flips) and routing_policy.yaml."""
        with self._state_lock:
            lenses_path = (self._dist_dir / "lenses.json").resolve()
            manifest_path = (self._dist_dir / "manifest.json").resolve()
            embed_path = (self._dist_dir / "embeddings.npz").resolve()
            shadow_path = (self._dist_dir / "l3_shadow_lenses.json").resolve()
            shadow_emb_path = (self._dist_dir / "l3_shadow_embeddings.npz").resolve()
            watched = (lenses_path, manifest_path, embed_path, shadow_path, shadow_emb_path, self._policy_path)
            key = tuple(
                (str(p), p.stat().st_mtime_ns if p.exists() else -1)
                for p in watched
            )
            if key == self._release_key and self._lenses:
                return False

            if not lenses_path.exists():
                raise FileNotFoundError(f"dist/current lenses.json not found at {lenses_path}")

            lenses_raw: list[dict[str, Any]] = json.loads(lenses_path.read_text(encoding="utf-8"))
            manifest = (
                json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest_path.exists()
                else {}
            )
            policy = (
                yaml.safe_load(self._policy_path.read_text(encoding="utf-8")) or {}
                if self._policy_path.exists()
                else {}
            )

            by_prim: dict[str, list[dict[str, Any]]] = {p: [] for p in PRIMITIVES}
            by_id: dict[str, dict[str, Any]] = {}
            docs_tokens: list[list[str]] = []
            for lens in lenses_raw:
                by_id[lens["id"]] = lens
                routing = lens.get("routing", {})
                prim = routing.get("primitive", "deconstruct")
                by_prim.setdefault(prim, []).append(lens)
                bm25_text = " ".join(
                    [
                        str(lens.get("title", "")),
                        str(lens.get("tagline", "")),
                        str(routing.get("pain_point", "")),
                        " ".join(routing.get("trigger_utterances") or []),
                        " ".join(routing.get("domains") or []),
                    ]
                )
                docs_tokens.append(_tokenize_zh_en(bm25_text))

            embed_ids: list[str] = []
            embed_matrix: np.ndarray | None = None
            if embed_path.exists():
                npz = np.load(embed_path, allow_pickle=False)
                embed_ids = [str(x) for x in npz["ids"].tolist()]
                embed_matrix = np.asarray(npz["vectors"], dtype=np.float32)

            shadow_raw: list[dict[str, Any]] = (
                json.loads(shadow_path.read_text(encoding="utf-8")) if shadow_path.exists() else []
            )
            shadow_docs_tokens: list[list[str]] = []
            for sl in shadow_raw:
                sr = sl.get("routing", {})
                s_text = " ".join(
                    [
                        str(sl.get("title", "")),
                        str(sl.get("title", "")),
                        str(sl.get("tagline", "")),
                        str(sr.get("pain_point", "")),
                        " ".join(sr.get("trigger_utterances") or []),
                        " ".join(sr.get("domains") or []),
                    ]
                )
                shadow_docs_tokens.append(_tokenize_zh_en(s_text))
            shadow_embed_ids: list[str] = []
            shadow_embed_matrix: np.ndarray | None = None
            if shadow_emb_path.exists():
                snpz = np.load(shadow_emb_path, allow_pickle=False)
                shadow_embed_ids = [str(x) for x in snpz["ids"].tolist()]
                shadow_embed_matrix = np.asarray(snpz["vectors"], dtype=np.float32)

            self._lenses = lenses_raw
            self._lenses_by_id = by_id
            self._lenses_by_primitive = by_prim
            self._embed_ids = embed_ids
            self._embed_idx_by_id = {cid: idx for idx, cid in enumerate(embed_ids)}
            self._embed_matrix = embed_matrix
            self._bm25 = _LightweightBM25(docs_tokens)
            self._shadow_lenses = shadow_raw
            self._shadow_embed_ids = shadow_embed_ids
            self._shadow_embed_matrix = shadow_embed_matrix
            self._shadow_bm25 = _LightweightBM25(shadow_docs_tokens) if shadow_docs_tokens else None
            self._policy = policy
            self._release_version = manifest.get("version")
            self._release_key = key
            logger.info(
                "Loaded %d L4 lenses + %d L3 shadow lenses (release=%s, embeddings=%s)",
                len(lenses_raw),
                len(shadow_raw),
                self._release_version,
                embed_matrix.shape if embed_matrix is not None else "none",
            )
            return True

    async def execute(self, request: RouteRequest) -> RouteResponse:
        start = time.perf_counter()
        self.reload_if_changed()
        t_after_reload = time.perf_counter()
        s2_params = self._stage2_params()

        trimmed_text = request.input_text.strip()[: self._max_input_chars]
        retrieval_text = _strip_style_modifiers(trimmed_text)
        vague_signal = _vague_signal_score(trimmed_text)

        # Stage 1: Hybrid Recall (BM25 + bge-m3 RRF) -> diverse, high-relevance candidates
        q_vec = await self._embed_query(retrieval_text)
        t_after_embed = time.perf_counter()
        sim_map: dict[str, float] = {}
        used_embed = False
        if q_vec is not None and self._embed_matrix is not None and len(self._embed_ids) > 0:
            sims = (self._embed_matrix @ q_vec).reshape(-1)
            sim_map = {cid: float(sims[idx]) for idx, cid in enumerate(self._embed_ids)}
            used_embed = True

        candidates = self._hybrid_recall_candidates(
            input_text=retrieval_text,
            app_name=request.app_name,
            sim_map=sim_map,
            vague_signal=vague_signal,
        )
        bt_cfg = self._policy.get("l3_breakthrough") or {}
        shadow_candidates = self._shadow_recall_candidates(
            input_text=retrieval_text,
            q_vec=q_vec,
            bt_cfg=bt_cfg,
        )

        # Evaluate active workflow session (score pinned lens in the same CE batch if needed)
        pinned_lens = self._get_pinned_workflow_lens(request.app_name)
        is_explicit_cont = _is_workflow_continuation(trimmed_text)
        extra_pinned_for_rerank: list[dict[str, Any]] = []
        cand_ids = {lens["id"] for _, lens in candidates}
        if pinned_lens is not None and not is_explicit_cont and pinned_lens["id"] not in cand_ids:
            extra_pinned_for_rerank = [pinned_lens]
        t_after_recall = time.perf_counter()

        # Stage 2A (Fastest & Most Accurate): In-process MPS Cross-Encoder bge-reranker-v2-m3
        used_fallback = False
        model_label = self._inference.model_name
        rerank_scores: list[float] | None = None
        ce_batch_size = 0
        if self._reranker is not None and candidates:
            all_docs = (
                [_rerank_doc_text(lens, query=retrieval_text) for _, lens in candidates]
                + [_rerank_doc_text(lens, query=retrieval_text) for _, lens in shadow_candidates]
                + [_rerank_doc_text(lens, query=retrieval_text) for lens in extra_pinned_for_rerank]
            )
            ce_batch_size = len(all_docs)
            rerank_scores = await self._reranker.score(retrieval_text, all_docs)
        t_after_rerank = time.perf_counter()

        pinned_ce_score: float | None = None
        if rerank_scores is not None and len(rerank_scores) == (
            len(candidates) + len(shadow_candidates) + len(extra_pinned_for_rerank)
        ):
            n_l4 = len(candidates)
            n_sh = len(shadow_candidates)
            l4_scores = rerank_scores[:n_l4]
            shadow_scores = rerank_scores[n_l4 : n_l4 + n_sh]
            if pinned_lens is not None:
                if extra_pinned_for_rerank:
                    pinned_ce_score = float(rerank_scores[-1])
                else:
                    for (_, lens), sc in zip(candidates, l4_scores):
                        if lens["id"] == pinned_lens["id"]:
                            pinned_ce_score = float(sc)
                            break

            model_label = self._reranker.model_name  # type: ignore[union-attr]
            cards, l4_raw_ce_by_id, low_conf_deficit = self._select_from_reranker(
                candidates=candidates,
                rerank_scores=l4_scores,
                app_name=request.app_name,
                vague_signal=vague_signal,
            )
            if shadow_candidates and shadow_scores:
                cards = self._apply_l3_breakthrough(
                    l4_cards=cards,
                    l4_raw_ce_by_id=l4_raw_ce_by_id,
                    shadow_candidates=shadow_candidates,
                    shadow_scores=shadow_scores,
                    bt_cfg=bt_cfg,
                    app_name=request.app_name,
                    vague_signal=vague_signal,
                    low_conf_deficit=low_conf_deficit,
                )
            h_prim, h_goal, h_clarity = self._heuristic_intent(request.app_name, trimmed_text)
            top_prim = cards[0].primitive if cards and cards[0].primitive else h_prim
            intent = IntentPrediction(
                primitive=top_prim,
                goal=h_goal,
                clarity=h_clarity,
                used_embed=used_embed,
            )
        else:
            # Stage 2B (Fallback): 2B LLM Rerank over top-12 candidates
            rerank_res = await self._rerank_with_2b(
                app_name=request.app_name,
                input_text=trimmed_text,
                candidates=candidates[:12],
            )
            t_after_rerank = time.perf_counter()
            if rerank_res is None:
                used_fallback = True
                prim, goal, clarity = self._heuristic_intent(request.app_name, trimmed_text)
                picked_indices: list[int] = []
            else:
                picked_indices, prim, goal, clarity = rerank_res
            cards = self._finalize_three_cards(
                candidates=candidates[:12],
                picked_indices=picked_indices,
            )
            intent = IntentPrediction(
                primitive=prim,
                goal=goal,
                clarity=clarity,
                used_embed=used_embed,
            )

        # Only pin workflow next-step when user continues the workflow or query is semantically aligned
        if pinned_lens is not None:
            should_pin = is_explicit_cont
            if not should_pin:
                if pinned_ce_score is not None:
                    should_pin = pinned_ce_score >= s2_params["workflow_min_rerank_logit"]
                else:
                    pinned_cos = sim_map.get(pinned_lens["id"], 0.0)
                    should_pin = pinned_cos >= s2_params["workflow_min_cosine"]

            if should_pin:
                pinned_card = self._to_card_item(pinned_lens, score=1.0, is_workflow_next=True)
                others = [c for c in cards if c.id != pinned_card.id]
                cards = [pinned_card] + others[: max(0, self._top_k - 1)]
            else:
                self._clear_workflow_session(request.app_name)

        t_end = time.perf_counter()
        latency_ms = round((t_end - start) * 1000.0, 2)
        reload_ms = (t_after_reload - start) * 1000.0
        embed_ms = (t_after_embed - t_after_reload) * 1000.0
        recall_ms = (t_after_recall - t_after_embed) * 1000.0
        rerank_ms = (t_after_rerank - t_after_recall) * 1000.0
        post_ms = (t_end - t_after_rerank) * 1000.0
        perf_line = (
            f"[PERF][route] app={request.app_name} chars={len(trimmed_text)} "
            f"ce_batch={ce_batch_size} total={latency_ms:.1f}ms "
            f"(reload={reload_ms:.1f} embed={embed_ms:.1f} recall={recall_ms:.1f} "
            f"rerank={rerank_ms:.1f} post={post_ms:.1f}) fallback={used_fallback}"
        )
        logger.info(perf_line)
        return RouteResponse(
            skill_type=self.name,
            cards=cards,
            latency_ms=latency_ms,
            model=model_label,
            used_fallback=used_fallback,
            intent=intent,
            release_version=self._release_version,
        )

    def _hybrid_recall_candidates(
        self,
        *,
        input_text: str,
        app_name: str,
        sim_map: dict[str, float],
        vague_signal: float = 0.0,
    ) -> list[tuple[float, dict[str, Any]]]:
        """Stage 1: Fuse bge-m3 cosine rank and denoised BM25 rank via RRF into a diverse pool."""
        n = len(self._lenses)
        if n == 0:
            return []

        bm25_scores = (
            self._bm25.score_all(_tokenize_zh_en(input_text))
            if self._bm25 is not None
            else [0.0] * n
        )
        bm25_order = sorted(range(n), key=lambda i: bm25_scores[i], reverse=True)
        bm25_rank = {idx: r + 1 for r, idx in enumerate(bm25_order)}

        if sim_map:
            emb_scores = [sim_map.get(self._lenses[i]["id"], 0.0) for i in range(n)]
            emb_order = sorted(range(n), key=lambda i: emb_scores[i], reverse=True)
            emb_rank = {idx: r + 1 for r, idx in enumerate(emb_order)}
        else:
            emb_rank = {}

        rrf_k_emb = 25.0
        rrf_k_bm25 = 35.0
        app_lower = app_name.strip().lower()
        scored: list[tuple[float, dict[str, Any]]] = []
        score_by_id: dict[str, float] = {}
        for idx, lens in enumerate(self._lenses):
            routing = lens.get("routing", {})
            meta = lens.get("meta", {})
            score = 0.0
            if emb_rank:
                score += 1.0 / (rrf_k_emb + emb_rank[idx])
            if bm25_scores[idx] > 0.0:
                score += 0.55 / (rrf_k_bm25 + bm25_rank[idx])
            if meta.get("scope") == "universal" or meta.get("shelf") == "evergreen":
                score += 0.004
            aff = [str(a).lower() for a in (routing.get("app_affinity") or [])]
            if app_lower and any(app_lower in a or a in app_lower for a in aff):
                score += 0.0025
            cat = str(meta.get("category", "general"))
            doms = [str(d).lower() for d in (routing.get("domains") or [])]
            if _app_category_bonus(app_lower, cat, doms) > 0.0:
                score += 0.002
            scored.append((score, lens))
            score_by_id[lens["id"]] = score

        scored.sort(key=lambda pair: pair[0], reverse=True)

        pool_limit = min(self._candidate_pool_size, len(scored))
        chosen: list[tuple[float, dict[str, Any]]] = []
        used_ids: set[str] = set()
        covered_prims: set[str] = set()

        # 1) Global Hybrid RRF winners (always reserve up to 3 slots for uncovered primitives)
        primary_limit = min(max(pool_limit - 3, 1), 12)
        for pair in scored[:primary_limit]:
            chosen.append(pair)
            used_ids.add(pair[1]["id"])
            covered_prims.add(str(pair[1].get("routing", {}).get("primitive", "deconstruct")))

        # 2) Ensure every uncovered primitive contributes its top hybrid match
        for pair in scored:
            if len(chosen) >= pool_limit:
                break
            lid = pair[1]["id"]
            prim = str(pair[1].get("routing", {}).get("primitive", "deconstruct"))
            if lid in used_ids or prim in covered_prims:
                continue
            chosen.append(pair)
            used_ids.add(lid)
            covered_prims.add(prim)

        # 3) Backfill remaining slots up to pool_limit
        for pair in scored:
            if len(chosen) >= pool_limit:
                break
            if pair[1]["id"] in used_ids:
                continue
            chosen.append(pair)
            used_ids.add(pair[1]["id"])

        # 4) Vague-query metacognitive safety net: ensure core clarification/explication anchors enter Stage 2
        if vague_signal >= 0.5:
            for anchor_id in VAGUE_ANCHOR_IDS:
                if anchor_id in used_ids:
                    continue
                lens = self._lenses_by_id.get(anchor_id)
                if lens is not None:
                    chosen.append((score_by_id.get(anchor_id, 0.0), lens))
                    used_ids.add(anchor_id)

        return chosen

    def _shadow_recall_candidates(
        self,
        *,
        input_text: str,
        q_vec: np.ndarray | None,
        bt_cfg: dict[str, Any],
    ) -> list[tuple[float, dict[str, Any]]]:
        """Stage 1 shadow probe over off_shelf L3 inventory (<0.3ms, reuses q_vec)."""
        if not bt_cfg.get("enabled", True) or not self._shadow_lenses:
            return []
        max_k = int(bt_cfg.get("max_candidates_to_rerank", 2))
        min_cos = float(bt_cfg.get("min_cosine", 0.45))
        exclude_manual = bool(bt_cfg.get("exclude_manual_off_shelf", True))

        sim_map: dict[str, float] = {}
        if q_vec is not None and self._shadow_embed_matrix is not None and len(self._shadow_embed_ids) > 0:
            sims = (self._shadow_embed_matrix @ q_vec).reshape(-1)
            sim_map = {cid: float(sims[idx]) for idx, cid in enumerate(self._shadow_embed_ids)}

        bm25_scores = (
            self._shadow_bm25.score_all(_tokenize_zh_en(input_text))
            if self._shadow_bm25 is not None
            else [0.0] * len(self._shadow_lenses)
        )

        scored: list[tuple[float, dict[str, Any]]] = []
        for idx, lens in enumerate(self._shadow_lenses):
            if exclude_manual and lens.get("_manual_off_shelf"):
                continue
            cos = sim_map.get(lens["id"], 0.0)
            bm = bm25_scores[idx]
            if sim_map and cos < min_cos and bm < 4.0:
                continue
            if not sim_map and bm <= 0.0:
                continue
            scored.append((cos if sim_map else bm, lens))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[:max_k]

    def _compute_stage2_score(
        self,
        *,
        ce_score: float,
        lens: dict[str, Any],
        app_lower: str,
        vague_signal: float,
        vague_anchor_set: set[str],
        low_conf_deficit: float = 0.0,
        s1_norm: float = 0.0,
        hybrid_rescue_weight: float = 0.0,
    ) -> float:
        """Compute unified Stage-2 adjusted score from raw CE logit and contextual priors."""
        params = self._stage2_params()
        routing = lens.get("routing", {})
        meta = lens.get("meta", {})
        prim = str(routing.get("primitive", "deconstruct"))
        cat = str(meta.get("category", "general"))
        doms = [str(d).lower() for d in (routing.get("domains") or [])]
        clarity_fit = set(routing.get("clarity_fit") or [])

        score = float(ce_score)
        if hybrid_rescue_weight > 0.0:
            score += hybrid_rescue_weight * s1_norm

        if meta.get("scope") == "universal" or meta.get("shelf") == "evergreen":
            score += params["evergreen_boost"] * (1.0 - 0.55 * low_conf_deficit)

        aff = [str(a).lower() for a in (routing.get("app_affinity") or [])]
        if app_lower and any(app_lower in a or a in app_lower for a in aff):
            score += params["app_affinity_boost"]
        score += _app_category_bonus(app_lower, cat, doms)

        if vague_signal >= 0.5:
            if lens["id"] in vague_anchor_set:
                score += 0.75 * vague_signal
            elif prim == "metacognize" and "vague" in clarity_fit:
                score += 0.25 * vague_signal

        base_w = float(meta.get("base_weight", 1.0))
        score += params["base_weight_scale"] * (min(max(base_w, 0.2), 2.0) - 1.0)
        return score

    def _apply_l3_breakthrough(
        self,
        *,
        l4_cards: list[CardItem],
        l4_raw_ce_by_id: dict[str, float],
        shadow_candidates: list[tuple[float, dict[str, Any]]],
        shadow_scores: list[float],
        bt_cfg: dict[str, Any],
        app_name: str = "Unknown",
        vague_signal: float = 0.0,
        low_conf_deficit: float = 0.0,
    ) -> list[CardItem]:
        """Allow at most 1 off_shelf L3 card to break through ONLY on overwhelming margin over L4."""
        if not l4_cards or not shadow_candidates or not shadow_scores:
            return l4_cards
        min_logit = float(bt_cfg.get("min_rerank_logit", -1.0))
        margin_top1 = float(bt_cfg.get("margin_over_top1", 1.5))
        margin_top3 = float(bt_cfg.get("margin_over_top3", 2.0))

        best_idx = max(range(len(shadow_scores)), key=lambda i: shadow_scores[i])
        best_logit = float(shadow_scores[best_idx])
        best_lens = shadow_candidates[best_idx][1]
        if best_logit < min_logit:
            return l4_cards

        # Compare raw CE logits directly so L4 evergreen/affinity priors don't skew margin gates
        raw_top1_logit = l4_raw_ce_by_id.get(l4_cards[0].id, float(l4_cards[0].score or 0.0))
        raw_top3_logit = l4_raw_ce_by_id.get(l4_cards[-1].id, float(l4_cards[-1].score or 0.0))
        l3_prim = str(best_lens.get("routing", {}).get("primitive", "deconstruct"))
        bt_score = self._compute_stage2_score(
            ce_score=best_logit,
            lens=best_lens,
            app_lower=app_name.strip().lower(),
            vague_signal=vague_signal,
            vague_anchor_set=set(VAGUE_ANCHOR_IDS),
            low_conf_deficit=low_conf_deficit,
        )

        # Case 1: Overwhelmingly beats L4 Slot 1 -> surface at Slot 1 (keep orthogonal Top-3)
        if best_logit - raw_top1_logit >= margin_top1:
            bt_display_score = max(bt_score, (l4_cards[0].score or 0.0) + 0.01)
            bt_card = self._to_card_item(best_lens, score=bt_display_score, is_l3_breakthrough=True)
            others = [c for c in l4_cards if c.id != bt_card.id and c.primitive != l3_prim]
            if len(others) < self._top_k - 1:
                others = [c for c in l4_cards if c.id != bt_card.id]
            return [bt_card] + others[: max(0, self._top_k - 1)]

        # Case 2: Overwhelmingly beats L4 Slot 3 & orthogonal to Slot 1/2 -> replace Slot 3 and keep monotonic order
        if len(l4_cards) >= self._top_k and best_logit - raw_top3_logit >= margin_top3:
            top2_prims = {c.primitive for c in l4_cards[: self._top_k - 1]}
            if l3_prim not in top2_prims:
                bt_card = self._to_card_item(best_lens, score=bt_score, is_l3_breakthrough=True)
                combined = l4_cards[: self._top_k - 1] + [bt_card]
                combined.sort(key=lambda c: float(c.score or 0.0), reverse=True)
                return combined

        return l4_cards

    async def inspect_recall(
        self,
        *,
        query: str,
        app_name: str = "Unknown",
        top_k: int = 20,
    ) -> dict[str, Any]:
        """Run the exact live 2-stage router over ALL L4 + L3 shadow lenses for Admin Recall Inspector."""
        self.reload_if_changed()
        trimmed = query.strip()[: self._max_input_chars]
        retrieval_text = _strip_style_modifiers(trimmed)
        vague_signal = _vague_signal_score(trimmed)
        app_lower = app_name.strip().lower()
        bt_cfg = self._policy.get("l3_breakthrough") or {}
        min_cos = float(bt_cfg.get("min_cosine", 0.45))
        min_logit = float(bt_cfg.get("min_rerank_logit", -1.0))
        margin_top1 = float(bt_cfg.get("margin_over_top1", 1.5))
        margin_top3 = float(bt_cfg.get("margin_over_top3", 2.0))
        exclude_manual = bool(bt_cfg.get("exclude_manual_off_shelf", True))

        q_vec = await self._embed_query(retrieval_text)
        l4_sim: dict[str, float] = {}
        if q_vec is not None and self._embed_matrix is not None and len(self._embed_ids) > 0:
            sims = (self._embed_matrix @ q_vec).reshape(-1)
            l4_sim = {cid: float(sims[idx]) for idx, cid in enumerate(self._embed_ids)}

        sh_sim: dict[str, float] = {}
        if q_vec is not None and self._shadow_embed_matrix is not None and len(self._shadow_embed_ids) > 0:
            ssims = (self._shadow_embed_matrix @ q_vec).reshape(-1)
            sh_sim = {cid: float(ssims[idx]) for idx, cid in enumerate(self._shadow_embed_ids)}

        q_toks = _tokenize_zh_en(retrieval_text)
        l4_bm = self._bm25.score_all(q_toks) if self._bm25 is not None else [0.0] * len(self._lenses)
        l4_bm_map = {self._lenses[i]["id"]: l4_bm[i] for i in range(len(self._lenses))}
        sh_bm = self._shadow_bm25.score_all(q_toks) if self._shadow_bm25 is not None else [0.0] * len(self._shadow_lenses)
        sh_bm_map = {self._shadow_lenses[i]["id"]: sh_bm[i] for i in range(len(self._shadow_lenses))}

        # Include live Stage-1 recall candidates first so live Top-3 is always inspected under identical scoring
        live_stage1 = self._hybrid_recall_candidates(
            input_text=retrieval_text,
            app_name=app_name,
            sim_map=l4_sim,
            vague_signal=vague_signal,
        )
        live_s1_map = {lens["id"]: float(s1) for s1, lens in live_stage1}
        s1_vals = [float(s1) for s1, _ in live_stage1]
        s1_min = min(s1_vals) if s1_vals else 0.0
        s1_span = (max(s1_vals) - s1_min) if s1_vals else 0.0

        l4_pool_raw = [
            (l4_sim.get(l["id"], 0.0) + (0.04 * min(l4_bm[i], 10.0)), l4_sim.get(l["id"], 0.0), l4_bm[i], l, "l4")
            for i, l in enumerate(self._lenses)
        ]
        l4_pool_raw.sort(key=lambda x: x[0], reverse=True)
        inspect_l4: list[tuple[float, float, float, dict[str, Any], str]] = []
        seen_inspect: set[str] = set()
        for _, lens in live_stage1:
            lid = lens["id"]
            if lid not in seen_inspect:
                seen_inspect.add(lid)
                inspect_l4.append((live_s1_map.get(lid, 0.0), l4_sim.get(lid, 0.0), l4_bm_map.get(lid, 0.0), lens, "l4"))
        for item in l4_pool_raw:
            if len(inspect_l4) >= max(20, len(live_stage1)):
                break
            lid = item[3]["id"]
            if lid not in seen_inspect:
                seen_inspect.add(lid)
                inspect_l4.append(item)

        sh_pool_raw = [
            (sh_sim.get(l["id"], 0.0) + (0.04 * min(sh_bm[i], 10.0)), sh_sim.get(l["id"], 0.0), sh_bm[i], l, "l3_shadow")
            for i, l in enumerate(self._shadow_lenses)
        ]
        sh_pool_raw.sort(key=lambda x: x[0], reverse=True)

        inspect_pool = inspect_l4 + sh_pool_raw[:10]
        rerank_scores: list[float] | None = None
        if self._reranker is not None and inspect_pool:
            docs = [_rerank_doc_text(item[3], query=retrieval_text) for item in inspect_pool]
            rerank_scores = await self._reranker.score(retrieval_text, docs)

        live_l4_ce = [
            float(rerank_scores[i])
            for i, item in enumerate(inspect_pool)
            if rerank_scores is not None and item[4] == "l4" and item[3]["id"] in live_s1_map
        ]
        max_ce = max(live_l4_ce) if live_l4_ce else 0.0
        low_conf_deficit = min(1.0, max(0.0, (-2.4 - max_ce) / 3.1)) if live_l4_ce else 0.0
        hybrid_rescue_weight = 4.5 * low_conf_deficit

        # Compute exact live Top-3 that macOS popup would show
        live_resp = await self.execute(RouteRequest(input_text=trimmed, app_name=app_name))
        live_l4_cards = [c for c in live_resp.cards if not c.is_l3_breakthrough and not c.is_workflow_next]
        ce_by_id = {
            inspect_pool[i][3]["id"]: float(rerank_scores[i])
            for i in range(len(inspect_pool))
            if rerank_scores is not None
        }
        ref_raw_top1 = ce_by_id.get(live_l4_cards[0].id, 0.0) if live_l4_cards else 0.0
        ref_raw_top3 = ce_by_id.get(live_l4_cards[-1].id, 0.0) if live_l4_cards else 0.0
        top2_prims = {c.primitive for c in live_resp.cards[: max(0, self._top_k - 1)]}
        shown_ids = {c.id for c in live_resp.cards}
        vague_anchor_set = set(VAGUE_ANCHOR_IDS)

        rows: list[dict[str, Any]] = []
        for idx, (_, cos, bm, lens, pool_kind) in enumerate(inspect_pool):
            meta = lens.get("meta", {})
            routing = lens.get("routing", {})
            ce = float(rerank_scores[idx]) if rerank_scores is not None else None
            if ce is not None:
                s1_val = live_s1_map.get(lens["id"])
                s1_norm = ((s1_val - s1_min) / s1_span) if (s1_val is not None and s1_span > 1e-9) else 0.0
                final_s = self._compute_stage2_score(
                    ce_score=ce,
                    lens=lens,
                    app_lower=app_lower,
                    vague_signal=vague_signal,
                    vague_anchor_set=vague_anchor_set,
                    low_conf_deficit=low_conf_deficit,
                    s1_norm=s1_norm,
                    hybrid_rescue_weight=hybrid_rescue_weight if s1_val is not None else 0.0,
                )
            else:
                final_s = cos

            bt_status = "l4"
            gap_top1 = None
            gap_top3 = None
            if pool_kind == "l3_shadow":
                manual_off = bool(lens.get("_manual_off_shelf"))
                if lens["id"] in shown_ids:
                    bt_status = "triggered"
                elif exclude_manual and manual_off:
                    bt_status = "manual_excluded"
                elif ce is not None:
                    gap_top1 = round((ref_raw_top1 + margin_top1) - ce, 2)
                    gap_top3 = round((ref_raw_top3 + margin_top3) - ce, 2)
                    if ce < min_logit or (sh_sim and cos < min_cos and bm < 4.0):
                        bt_status = "below_floor"
                    elif gap_top3 <= 0 and str(routing.get("primitive", "")) in top2_prims:
                        bt_status = "blocked_orthogonal"
                    elif gap_top1 > 0 and gap_top3 > 0:
                        bt_status = "below_margin"
                    else:
                        bt_status = "eligible_gap"

            rows.append(
                {
                    "id": lens["id"],
                    "title": lens.get("title", ""),
                    "tagline": lens.get("tagline", ""),
                    "primitive": routing.get("primitive", "deconstruct"),
                    "category": meta.get("category", "general"),
                    "tier": meta.get("tier", "B"),
                    "shelf": meta.get("shelf", "on_shelf"),
                    "source": meta.get("source", ""),
                    "manual_off_shelf": bool(lens.get("_manual_off_shelf")),
                    "cosine": round(cos, 4),
                    "bm25": round(bm, 2),
                    "rerank_logit": round(ce, 3) if ce is not None else None,
                    "final_score": round(final_s, 3),
                    "in_live_top3": lens["id"] in shown_ids,
                    "breakthrough_status": bt_status,
                    "breakthrough_gap_top1": gap_top1,
                    "breakthrough_gap_top3": gap_top3,
                    "trigger_utterances": routing.get("trigger_utterances") or [],
                }
            )

        rows.sort(key=lambda r: r["final_score"], reverse=True)
        for rank_idx, row in enumerate(rows[:top_k], start=1):
            row["rank"] = rank_idx

        return {
            "query": trimmed,
            "cleaned_query": retrieval_text,
            "release_version": self._release_version,
            "reranker_active": rerank_scores is not None,
            "breakthrough_policy": {
                "enabled": bt_cfg.get("enabled", True),
                "min_cosine": min_cos,
                "min_rerank_logit": min_logit,
                "margin_over_top1": margin_top1,
                "margin_over_top3": margin_top3,
                "exclude_manual_off_shelf": exclude_manual,
            },
            "would_show_top3": [c.model_dump() for c in live_resp.cards],
            "ranked_all": rows[:top_k],
        }

    def _select_from_reranker(
        self,
        *,
        candidates: list[tuple[float, dict[str, Any]]],
        rerank_scores: list[float],
        app_name: str,
        vague_signal: float = 0.0,
    ) -> tuple[list[CardItem], dict[str, float], float]:
        """Rank candidates with Cross-Encoder + denoised Stage-1 rescue + Quality-Gated Orthogonality."""
        params = self._stage2_params()
        app_lower = app_name.strip().lower()
        max_ce = max(rerank_scores) if rerank_scores else 0.0

        # Denoised Stage-1 Hybrid rescue for deep low-confidence OOV short queries
        s1_vals = [float(s1) for s1, _ in candidates]
        s1_min = min(s1_vals) if s1_vals else 0.0
        s1_span = (max(s1_vals) - s1_min) if s1_vals else 0.0
        low_conf_deficit = min(1.0, max(0.0, (-2.4 - max_ce) / 3.1))
        hybrid_rescue_weight = 4.5 * low_conf_deficit

        vague_anchor_set = set(VAGUE_ANCHOR_IDS)
        raw_ce_by_id: dict[str, float] = {}
        final_scored: list[tuple[float, dict[str, Any]]] = []
        for (s1_score, lens), ce_score in zip(candidates, rerank_scores):
            raw_ce_by_id[lens["id"]] = float(ce_score)
            s1_norm = ((float(s1_score) - s1_min) / s1_span) if s1_span > 1e-9 else 0.0
            score = self._compute_stage2_score(
                ce_score=float(ce_score),
                lens=lens,
                app_lower=app_lower,
                vague_signal=vague_signal,
                vague_anchor_set=vague_anchor_set,
                low_conf_deficit=low_conf_deficit,
                s1_norm=s1_norm,
                hybrid_rescue_weight=hybrid_rescue_weight,
            )
            final_scored.append((score, lens))

        final_scored.sort(key=lambda pair: pair[0], reverse=True)
        if not final_scored:
            return [], raw_ce_by_id, low_conf_deficit

        def _is_universal(lens: dict[str, Any]) -> bool:
            m = lens.get("meta", {})
            return m.get("scope") == "universal" or m.get("shelf") == "evergreen"

        cards: list[CardItem] = []
        used_ids: set[str] = set()
        used_prims: Counter[str] = Counter()

        top1_score, top1_lens = final_scored[0]
        top1_prim = str(top1_lens.get("routing", {}).get("primitive", "deconstruct"))
        used_ids.add(top1_lens["id"])
        used_prims[top1_prim] += 1
        cards.append(self._to_card_item(top1_lens, score=top1_score, is_exploration=False))

        cliff_floor = params["cliff_score_floor"]
        cliff_margin = params["cliff_margin_below_top1"]

        while len(cards) < self._top_k:
            best_ortho: tuple[float, dict[str, Any]] | None = None
            best_univ_ortho: tuple[float, dict[str, Any]] | None = None

            for sc, lens in final_scored:
                lid = lens["id"]
                if lid in used_ids:
                    continue
                prim = str(lens.get("routing", {}).get("primitive", "deconstruct"))
                if used_prims[prim] == 0:
                    if best_ortho is None:
                        best_ortho = (sc, lens)
                    if best_univ_ortho is None and _is_universal(lens) and lid != "pt_brainstorm":
                        best_univ_ortho = (sc, lens)

            chosen_pair: tuple[float, dict[str, Any]] | None = None
            if best_ortho is not None:
                bo_score, bo_lens = best_ortho
                in_cliff = bo_score < cliff_floor and (top1_score - bo_score) > cliff_margin
                if in_cliff and not _is_universal(bo_lens) and best_univ_ortho is not None:
                    chosen_pair = best_univ_ortho
                else:
                    chosen_pair = best_ortho
            else:
                for sc, lens in final_scored:
                    if lens["id"] not in used_ids:
                        chosen_pair = (sc, lens)
                        break

            if chosen_pair is None:
                break
            sc, lens = chosen_pair
            prim = str(lens.get("routing", {}).get("primitive", "deconstruct"))
            used_ids.add(lens["id"])
            used_prims[prim] += 1
            cards.append(self._to_card_item(lens, score=sc, is_exploration=False))

        return cards, raw_ce_by_id, low_conf_deficit

    async def _rerank_with_2b(
        self,
        *,
        app_name: str,
        input_text: str,
        candidates: list[tuple[float, dict[str, Any]]],
    ) -> tuple[list[int], str, str, str] | None:
        if not candidates:
            return None
        lines = [f"App: {app_name}", f"Text: {input_text}", "候选:"]
        for idx, (_, lens) in enumerate(candidates, start=1):
            prim = lens.get("routing", {}).get("primitive", "deconstruct")
            title = lens.get("title", "")
            tagline = _compact_preview(str(lens.get("tagline") or lens.get("routing", {}).get("pain_point") or ""), 34)
            lines.append(f"{idx}|{prim}|{title}|{tagline}")
        user_prompt = "\n".join(lines)

        try:
            raw = await self._inference.chat_json(
                system_prompt=SYSTEM_PROMPT_V2,
                user_prompt=user_prompt,
                json_schema=RERANK_JSON_SCHEMA,
                num_predict=56,
            )
            return self._parse_rerank_json(raw, len(candidates))
        except Exception as exc:
            logger.warning("2B rerank failed, falling back to hybrid top-3: %s", exc)
            return None

    @staticmethod
    def _parse_rerank_json(raw: str, max_idx: int) -> tuple[list[int], str, str, str] | None:
        if not raw:
            return None
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except ValueError:
            return None

        raw_picks = payload.get("picks")
        picks: list[int] = []
        if isinstance(raw_picks, list):
            for item in raw_picks:
                try:
                    num = int(item)
                except (TypeError, ValueError):
                    continue
                if 1 <= num <= max_idx and num not in picks:
                    picks.append(num)
        if not picks:
            return None

        prim = str(payload.get("primitive", "")).strip().lower()
        goal = str(payload.get("goal", "")).strip().lower()
        clarity = str(payload.get("clarity", "")).strip().lower()
        if prim not in PRIMITIVES:
            prim = "deconstruct"
        if goal not in GOALS:
            goal = "optimize"
        if clarity not in CLARITIES:
            clarity = "concrete"
        return picks, prim, goal, clarity

    def _finalize_three_cards(
        self,
        *,
        candidates: list[tuple[float, dict[str, Any]]],
        picked_indices: list[int],
    ) -> list[CardItem]:
        cards: list[CardItem] = []
        used_ids: set[str] = set()
        used_prims: set[str] = set()

        deferred_same_prim: list[tuple[float, dict[str, Any]]] = []
        for idx in picked_indices:
            score, lens = candidates[idx - 1]
            lid = lens["id"]
            prim = str(lens.get("routing", {}).get("primitive", "deconstruct"))
            if lid in used_ids:
                continue
            if prim in used_prims:
                deferred_same_prim.append((score, lens))
                continue
            used_ids.add(lid)
            used_prims.add(prim)
            cards.append(self._to_card_item(lens, score=score, is_exploration=False))
            if len(cards) >= self._top_k:
                return cards

        for score, lens in candidates:
            lid = lens["id"]
            prim = str(lens.get("routing", {}).get("primitive", "deconstruct"))
            if lid in used_ids or prim in used_prims:
                continue
            used_ids.add(lid)
            used_prims.add(prim)
            cards.append(self._to_card_item(lens, score=score, is_exploration=False))
            if len(cards) >= self._top_k:
                return cards

        for score, lens in deferred_same_prim + candidates:
            if lens["id"] in used_ids:
                continue
            used_ids.add(lens["id"])
            cards.append(self._to_card_item(lens, score=score, is_exploration=False))
            if len(cards) >= self._top_k:
                break

        return cards

    async def _embed_query(self, text: str) -> np.ndarray | None:
        embed_fn = getattr(self._inference, "embed", None)
        if embed_fn is None:
            return None
        try:
            vec = await embed_fn(text)
            if not vec:
                return None
            arr = np.asarray(vec, dtype=np.float32)
            norm = float(np.linalg.norm(arr))
            if norm == 0.0:
                return None
            return arr / norm
        except Exception as exc:
            logger.warning("bge-m3 embedding failed, degrading to BM25 scoring: %s", exc)
            return None

    @staticmethod
    def _heuristic_intent(app_name: str, text: str) -> tuple[str, str, str]:
        lower_app = app_name.lower()
        lower_text = text.lower()
        if any(k in lower_text for k in ("选a还是", "选哪个", "纠结", "对比", "取舍", "vs", "offer")):
            return "converge", "evaluate", "conflicted"
        if any(k in lower_text for k in ("报错", "error", "exception", "traceback", "bug", "故障", "timeout")):
            return "critique", "debug", "concrete"
        if any(k in lower_text for k in ("总结", "摘要", "太长", "提炼", "tl;dr", "纪要")):
            return "compress", "digest", "concrete"
        if any(k in lower_text for k in ("去ai味", "润色", "重写", "改写", "翻译", "邮件")):
            return "translate", "optimize", "concrete"
        if any(k in lower_text for k in ("没头绪", "不知道", "模糊", "想法", "方向", "调研")):
            return "metacognize", "create", "vague"
        if any(k in lower_app for k in ("cursor", "code", "xcode", "terminal", "iterm", "warp")):
            return "deconstruct", "optimize", "concrete"
        return "deconstruct", "optimize", "vague" if len(text) < 25 else "concrete"

    @staticmethod
    def _to_card_item(
        lens: dict[str, Any],
        *,
        score: float | None = None,
        is_exploration: bool = False,
        is_workflow_next: bool = False,
        is_l3_breakthrough: bool = False,
    ) -> CardItem:
        routing = lens.get("routing", {})
        execution = lens.get("execution", {})
        meta = lens.get("meta", {})
        wf = execution.get("workflow") or {}

        action_type = str(execution.get("action_type", "inject_prompt"))
        inject_mode = execution.get("inject_mode")
        mode: str = "wrap" if inject_mode == "wrap" else "append"
        content = str(execution.get("content", "")).strip()
        variables = [str(v) for v in (execution.get("variables") or [])]

        prompt_text = content
        if action_type == "inject_prompt" and variables:
            prompt_text = content + ZERO_FRICTION_VAR_NOTE
        if mode == "append" and prompt_text and not prompt_text.startswith("\n"):
            prompt_text = "\n\n" + prompt_text

        prim = str(routing.get("primitive", "deconstruct"))
        tagline = str(lens.get("tagline") or routing.get("pain_point") or lens.get("title", ""))

        return CardItem(
            id=str(lens["id"]),
            title=str(lens["title"]),
            dimension=prim,
            intent=tagline,
            mode=mode,  # type: ignore[arg-type]
            prompt=prompt_text,
            preview=_compact_preview(tagline),
            action_type=action_type,
            primitive=prim,
            source=meta.get("source"),
            score=round(score, 4) if score is not None else None,
            is_exploration=is_exploration,
            variables=variables,
            workflow_id=wf.get("workflow_id"),
            workflow_step=wf.get("step"),
            is_workflow_next=is_workflow_next,
            is_l3_breakthrough=is_l3_breakthrough,
        )
