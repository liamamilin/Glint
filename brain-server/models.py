"""Pydantic schemas for Glint brain-server HTTP API (v2.0 backward-compatible with v0.1 macos-app)."""

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


class IntentPrediction(BaseModel):
    primitive: str
    goal: str
    clarity: str
    used_embed: bool = False


class CardItem(BaseModel):
    # v0.1 core fields (kept strictly compatible with macos-app)
    id: str
    title: str
    dimension: str
    intent: str
    mode: Literal["append", "wrap"]
    prompt: str
    preview: str = Field(default="", description="Single-line compact preview for the floating card UI")

    # v2.0 optional metadata extensions
    action_type: str = "inject_prompt"
    primitive: str | None = None
    source: str | None = None
    score: float | None = None
    is_exploration: bool = False
    variables: list[str] = Field(default_factory=list)
    workflow_id: str | None = None
    workflow_step: int | None = None
    is_workflow_next: bool = False
    is_l3_breakthrough: bool = False


class RouteResponse(BaseModel):
    skill_type: str = "cognitive_cards"
    cards: list[CardItem]
    latency_ms: float
    model: str
    used_fallback: bool = False
    intent: IntentPrediction | None = None
    release_version: str | None = None


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
    predicted_primitive: str | None = Field(default=None)
    predicted_goal: str | None = Field(default=None)
    predicted_clarity: str | None = Field(default=None)
    release_version: str | None = Field(default=None)


class FeedbackResponse(BaseModel):
    ok: bool = True
    record_id: int


class HealthResponse(BaseModel):
    status: str
    model: str
    embed_model: str = "bge-m3:latest"
    cards_loaded: int
    ollama_reachable: bool
    router_mode: str = "v2_dual_path"
    release_version: str | None = None
