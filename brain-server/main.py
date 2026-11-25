"""FastAPI entrypoint for the Glint local semantic router (v2.0 dual-path + v0.1 fallback)."""

from __future__ import annotations

import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from config import Settings, settings
from logger import UsageLogger
from models import (
    CardItem,
    FeedbackRequest,
    FeedbackResponse,
    HealthResponse,
    RouteRequest,
    RouteResponse,
    SeedConfigModel,
    SeedDrawRequest,
    SeedDrawResponse,
    SeedFeedbackRequest,
    SeedFeedbackResponse,
    SeedItem,
    SeedPoolMode,
    SeedPoolResponse,
    SeedStateEntry,
    SeedStateResponse,
)
from reranker import LocalBgeReranker, RerankerClient
from router import InferenceClient, OllamaClient, SemanticRouter
from seed_store import SeedStore
from skills.cognitive_cards import CognitiveCardsSkill
from skills.lens_router import LensRouterSkill

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("glint.main")


def create_app(
    cfg: Settings = settings,
    inference_client: InferenceClient | None = None,
    reranker_client: RerankerClient | None = None,
) -> FastAPI:
    inference = inference_client or OllamaClient(cfg)
    reranker: RerankerClient | None = reranker_client
    if reranker is None and cfg.enable_reranker and inference_client is None:
        reranker = LocalBgeReranker(model_name=cfg.reranker_model)
    usage_logger = UsageLogger(cfg.db_path)
    seed_store = SeedStore(cfg.seed_state_path)

    use_v2 = cfg.dist_dir is not None and (cfg.dist_dir / "lenses.json").exists()
    if use_v2:
        cognitive_skill: LensRouterSkill | CognitiveCardsSkill = LensRouterSkill(
            dist_dir=cfg.dist_dir,  # type: ignore[arg-type]
            policy_path=cfg.policy_path,
            inference=inference,
            reranker=reranker,
            top_k=cfg.top_k_cards,
            max_input_chars=cfg.max_input_chars,
        )
        router_mode = "v2_hybrid_rerank" if reranker is not None else "v2_dual_path"
        logger.info("Using v2.0 LensRouterSkill (%s) backed by %s", router_mode, cfg.dist_dir)
    else:
        cognitive_skill = CognitiveCardsSkill(
            cards_path=cfg.cards_path,
            inference=inference,
            top_k=cfg.top_k_cards,
            max_input_chars=cfg.max_input_chars,
        )
        router_mode = "v1_legacy_cards"
        logger.info("Using legacy CognitiveCardsSkill backed by %s", cfg.cards_path)

    semantic_router = SemanticRouter(
        skills={
            cognitive_skill.name: cognitive_skill,
            # Phase 2+ reserved extension slots:
            # "instant_summary": InstantSummarySkill(...),
            # "local_lookup": LocalLookupSkill(...),
        },
        default_skill=cognitive_skill.name,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if cfg.warmup_on_startup:
            await inference.warmup(cognitive_skill.system_prompt)
            if reranker is not None:
                await reranker.warmup()
        try:
            yield
        finally:
            await inference.close()

    app = FastAPI(title="Glint Brain Server", version="0.2.0", lifespan=lifespan)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        ollama_ok = await inference.is_healthy()
        release_ver = getattr(cognitive_skill, "release_version", None)
        return HealthResponse(
            status="ok",
            model=inference.model_name,
            embed_model=cfg.embed_model,
            cards_loaded=len(cognitive_skill.cards),
            ollama_reachable=ollama_ok,
            router_mode=router_mode,
            release_version=release_ver,
        )

    @app.get("/cards", response_model=list[CardItem])
    async def list_cards() -> list[CardItem]:
        return cognitive_skill.cards

    @app.post("/warmup")
    async def warmup() -> dict[str, bool]:
        await inference.warmup(cognitive_skill.system_prompt)
        if reranker is not None:
            await reranker.warmup()
        return {"ok": True}

    @app.post("/route", response_model=RouteResponse)
    async def route(request: RouteRequest) -> RouteResponse:
        return await semantic_router.route(request)

    @app.post("/feedback", response_model=FeedbackResponse)
    async def feedback(request: FeedbackRequest) -> FeedbackResponse:
        record_id = usage_logger.log_feedback(request)
        record_fn = getattr(cognitive_skill, "record_selection", None)
        if record_fn is not None:
            record_fn(request.app_name, request.selected_id)
        return FeedbackResponse(ok=True, record_id=record_id)

    def _seed_layers() -> tuple[list[dict], list[dict]]:
        raw_l4 = getattr(cognitive_skill, "raw_lenses", None)
        raw_l3 = getattr(cognitive_skill, "raw_shadow_lenses", None)
        if raw_l4 is not None:
            return raw_l4, (raw_l3 or [])
        # Legacy fallback for v0.1 CognitiveCardsSkill
        legacy = [
            {
                "id": c.id,
                "title": c.title,
                "tagline": c.preview or c.intent,
                "routing": {"pain_point": c.intent, "primitive": c.primitive or c.dimension},
                "execution": {"content": c.prompt},
                "meta": {"category": c.dimension, "tier": "A", "source": c.source or "cards"},
            }
            for c in cognitive_skill.cards
        ]
        return legacy, []

    @app.get("/seed/pool", response_model=SeedPoolResponse)
    async def seed_pool(mode: SeedPoolMode = "smart_mix") -> SeedPoolResponse:
        l4_lenses, l3_lenses = _seed_layers()
        recall_counts = usage_logger.get_recall_counts(days=30)
        scored, muted_count, snoozed_count = seed_store.score_candidates(
            l4_lenses=l4_lenses,
            l3_lenses=l3_lenses,
            recall_counts=recall_counts,
            mode=mode,
        )
        return SeedPoolResponse(
            items=[SeedItem(**s) for s in scored],
            mode=mode,
            total_l4=len(l4_lenses),
            total_l3=len(l3_lenses),
            muted_count=muted_count,
            snoozed_count=snoozed_count,
            release_version=getattr(cognitive_skill, "release_version", None),
        )

    @app.post("/seed/next", response_model=SeedDrawResponse)
    async def seed_next(request: SeedDrawRequest) -> SeedDrawResponse:
        l4_lenses, l3_lenses = _seed_layers()
        recall_counts = usage_logger.get_recall_counts(days=30)
        picked, eligible_count, muted_count, snoozed_count = seed_store.draw_next(
            l4_lenses=l4_lenses,
            l3_lenses=l3_lenses,
            recall_counts=recall_counts,
            mode=request.mode,
            exclude_ids=set(request.exclude_ids),
        )
        return SeedDrawResponse(
            item=SeedItem(**picked) if picked is not None else None,
            mode=request.mode,
            eligible_count=eligible_count,
            total_l4=len(l4_lenses),
            total_l3=len(l3_lenses),
            muted_count=muted_count,
            snoozed_count=snoozed_count,
            release_version=getattr(cognitive_skill, "release_version", None),
        )

    @app.post("/seed/feedback", response_model=SeedFeedbackResponse)
    async def seed_feedback(request: SeedFeedbackRequest) -> SeedFeedbackResponse:
        muted_count, snoozed_count = seed_store.apply_action(
            lens_id=request.id,
            action=request.action,
            snooze_days=request.snooze_days,
            category=request.category,
        )
        return SeedFeedbackResponse(ok=True, muted_count=muted_count, snoozed_count=snoozed_count)

    @app.post("/seed/reset", response_model=SeedFeedbackResponse)
    async def seed_reset() -> SeedFeedbackResponse:
        muted_count, snoozed_count = seed_store.reset_all()
        return SeedFeedbackResponse(ok=True, muted_count=muted_count, snoozed_count=snoozed_count)

    @app.get("/seed/state", response_model=SeedStateResponse)
    async def seed_state() -> SeedStateResponse:
        l4_lenses, l3_lenses = _seed_layers()
        index: dict[str, tuple[dict, str]] = {}
        for lens in l3_lenses:
            index[str(lens.get("id", ""))] = (lens, "L3")
        for lens in l4_lenses:
            index[str(lens.get("id", ""))] = (lens, "L4")

        st = seed_store.get_state_snapshot()
        now = time.time()
        hidden_items: list[SeedStateEntry] = []
        for lid in st["muted_ids"]:
            lens, layer = index.get(lid, ({}, "L4"))
            meta = lens.get("meta") or {}
            hidden_items.append(
                SeedStateEntry(
                    id=lid,
                    title=str(lens.get("title") or lid),
                    layer=layer,
                    tier=str(meta.get("tier") or "B"),
                    category=str(meta.get("category") or "general"),
                    status="muted",
                )
            )
        for lid, until_ts in st["snoozed_until"].items():
            lens, layer = index.get(lid, ({}, "L4"))
            meta = lens.get("meta") or {}
            rem_days = max(0.1, round((float(until_ts) - now) / 86400.0, 1))
            hidden_items.append(
                SeedStateEntry(
                    id=lid,
                    title=str(lens.get("title") or lid),
                    layer=layer,
                    tier=str(meta.get("tier") or "B"),
                    category=str(meta.get("category") or "general"),
                    status="snoozed",
                    remaining_days=rem_days,
                )
            )

        return SeedStateResponse(
            config=SeedConfigModel(**st["config"]),
            hidden_items=hidden_items,
            skip_counts=st["skip_counts"],
            category_interest=st["category_interest"],
            recent_shown_count=len(st["recent_shown"]),
            total_l4=len(l4_lenses),
            total_l3=len(l3_lenses),
        )

    @app.put("/seed/config", response_model=SeedConfigModel)
    async def seed_update_config(request: SeedConfigModel) -> SeedConfigModel:
        updated = seed_store.update_config(request.model_dump())
        return SeedConfigModel(**updated)

    # Mount unified Glint Management Console at /admin (zero extra process needed!)
    recog_dir = Path(__file__).resolve().parent.parent / "recognition-library"
    if recog_dir.exists():
        if str(recog_dir) not in sys.path:
            sys.path.insert(0, str(recog_dir))
        try:
            from admin.server import create_admin_app

            app.mount("/admin", create_admin_app(router_skill=cognitive_skill))
            logger.info("Mounted unified Glint Management Console at /admin")
        except Exception as exc:
            logger.warning("Could not mount /admin console: %s", exc)

    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )
