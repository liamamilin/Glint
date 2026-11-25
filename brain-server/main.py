"""FastAPI entrypoint for the Glint local semantic router (v2.0 dual-path + v0.1 fallback)."""

from __future__ import annotations

import logging
import sys
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
)
from reranker import LocalBgeReranker, RerankerClient
from router import InferenceClient, OllamaClient, SemanticRouter
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
