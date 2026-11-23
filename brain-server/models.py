"""Pydantic schemas for Glint brain-server HTTP API."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class RouteRequest(BaseModel):
    input_text: str = Field(..., min_length=1, description="Selected text or current input snippet")
    app_name: str = Field(default="Unknown", description="Frontmost macOS application name")
    skill_hint: str | None = Field(
        default=None,
        description="Optional explicit skill override (Phase 1 defaults to cognitive_cards)",
    )


class CardItem(BaseModel):
    id: str
    title: str
    dimension: str
    intent: str
    mode: Literal["append", "wrap"]
    prompt: str
    preview: str = Field(default="", description="Single-line compact preview for the floating card UI")


class RouteResponse(BaseModel):
    skill_type: str = "cognitive_cards"
    cards: list[CardItem]
    latency_ms: float
    model: str
    used_fallback: bool = False


class FeedbackRequest(BaseModel):
    app_name: str = Field(default="Unknown")
    skill_type: str = Field(default="cognitive_cards")
    input_text: str = Field(..., min_length=1)
    recommended_ids: list[str] = Field(default_factory=list)
    selected_id: str | None = Field(
        default=None,
        description="Selected card ID, '__custom__' when user typed custom intent, or null if cancelled with Esc",
    )
    inject_mode: Literal["append", "wrap"] | None = Field(default=None)
    latency_ms: float | None = Field(default=None)
    custom_intent: str | None = Field(
        default=None,
        description="Free-text intent typed by the user when none of the 3 cards satisfied them",
    )


class FeedbackResponse(BaseModel):
    ok: bool = True
    record_id: int


class HealthResponse(BaseModel):
    status: str
    model: str
    cards_loaded: int
    ollama_reachable: bool
