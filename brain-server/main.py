"""FastAPI entrypoint for the Glint local semantic router."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

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
from router import InferenceClient, OllamaClient, SemanticRouter
from skills.cognitive_cards import CognitiveCardsSkill

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def create_app(
    cfg: Settings = settings,
    inference_client: InferenceClient | None = None,
) -> FastAPI:
    inference = inference_client or OllamaClient(cfg)
    usage_logger = UsageLogger(cfg.db_path)
    cognitive_skill = CognitiveCardsSkill(
        cards_path=cfg.cards_path,
        inference=inference,
        top_k=cfg.top_k_cards,
        max_input_chars=cfg.max_input_chars,
    )
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
        try:
            yield
        finally:
            await inference.close()

    app = FastAPI(title="Glint Brain Server", version="0.1.0", lifespan=lifespan)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        ollama_ok = await inference.is_healthy()
        return HealthResponse(
            status="ok",
            model=inference.model_name,
            cards_loaded=len(cognitive_skill.cards),
            ollama_reachable=ollama_ok,
        )

    @app.get("/cards", response_model=list[CardItem])
    async def list_cards() -> list[CardItem]:
        return cognitive_skill.cards

    @app.post("/route", response_model=RouteResponse)
    async def route(request: RouteRequest) -> RouteResponse:
        return await semantic_router.route(request)

    @app.post("/feedback", response_model=FeedbackResponse)
    async def feedback(request: FeedbackRequest) -> FeedbackResponse:
        record_id = usage_logger.log_feedback(request)
        return FeedbackResponse(ok=True, record_id=record_id)

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
