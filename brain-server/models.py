"""Pydantic schemas for Glint brain-server HTTP API (v2.0 backward-compatible with v0.1 macos-app)."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, field_validator


class RouteRequest(BaseModel):
    input_text: str = Field(..., min_length=1, description="Selected text or current input snippet")
    app_name: str = Field(default="Unknown", description="Frontmost macOS application name")
    skill_hint: str | None = Field(
        default=None,
        description="Optional explicit skill override (Phase 1 defaults to cognitive_cards)",
    )

    @field_validator("input_text")
    @classmethod
    def _validate_non_blank_input(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input_text must not be empty or whitespace-only")
        return value


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

    @field_validator("input_text")
    @classmethod
    def _validate_non_blank_input(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input_text must not be empty or whitespace-only")
        return value
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


SeedPoolMode = Literal["smart_mix", "pinned_only", "l4_all", "l3_discovery", "tier_s"]


class SeedItem(BaseModel):
    id: str
    title: str
    tagline: str = ""
    pain_point: str = ""
    prompt: str = ""
    primitive: str = "metacognize"
    category: str = "general"
    tier: str = "B"
    layer: Literal["L4", "L3"] = "L4"
    source: str = ""
    trigger_utterances: list[str] = Field(default_factory=list)
    weight: float = 1.0
    probability_pct: float = 0.0
    recall_count_30d: int = 0
    reason_tag: str = ""
    is_pinned: bool = False


class SeedPoolResponse(BaseModel):
    items: list[SeedItem]
    mode: SeedPoolMode = "smart_mix"
    total_l4: int
    total_l3: int = 0
    muted_count: int
    snoozed_count: int
    pinned_count: int = 0
    release_version: str | None = None


class SeedDrawRequest(BaseModel):
    mode: SeedPoolMode = "smart_mix"
    exclude_ids: list[str] = Field(default_factory=list)


class SeedDrawResponse(BaseModel):
    item: SeedItem | None = None
    mode: SeedPoolMode = "smart_mix"
    eligible_count: int = 0
    total_l4: int = 0
    total_l3: int = 0
    muted_count: int = 0
    snoozed_count: int = 0
    pinned_count: int = 0
    release_version: str | None = None


class SeedFeedbackRequest(BaseModel):
    id: str = Field(..., min_length=1)
    action: Literal["mute", "snooze", "unmute", "skip", "expand", "copy", "pin", "unpin"]
    snooze_days: int = Field(default=7, ge=1, le=365)
    category: str | None = None


class SeedFeedbackResponse(BaseModel):
    ok: bool = True
    muted_count: int
    snoozed_count: int
    pinned_count: int = 0


class SeedPinnedUpdateRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)
    mode: Literal["add", "remove", "replace"] = "add"


class SeedPinnedUpdateResponse(BaseModel):
    ok: bool = True
    pinned_ids: list[str] = Field(default_factory=list)
    pinned_count: int = 0


class SeedConfigModel(BaseModel):
    tier_s_weight: float = Field(default=4.0, ge=0.1, le=20.0)
    tier_a_weight: float = Field(default=2.0, ge=0.1, le=20.0)
    tier_b_weight: float = Field(default=1.0, ge=0.1, le=20.0)
    l3_smart_mix_share: float = Field(default=0.15, ge=0.0, le=0.8)
    pinned_smart_mix_share: float = Field(default=0.30, ge=0.0, le=0.75)
    recall_decay_rate: float = Field(default=0.35, ge=0.0, le=2.0)
    skip_decay_factor: float = Field(default=0.72, ge=0.2, le=1.0)


class SeedStateEntry(BaseModel):
    id: str
    title: str
    layer: str = "L4"
    tier: str = "B"
    category: str = "general"
    status: Literal["muted", "snoozed", "pinned"]
    remaining_days: float | None = None


class SeedStateResponse(BaseModel):
    config: SeedConfigModel
    hidden_items: list[SeedStateEntry] = Field(default_factory=list)
    pinned_items: list[SeedStateEntry] = Field(default_factory=list)
    pinned_ids: list[str] = Field(default_factory=list)
    skip_counts: dict[str, int] = Field(default_factory=dict)
    category_interest: dict[str, int] = Field(default_factory=dict)
    recent_shown_count: int = 0
    total_l4: int = 0
    total_l3: int = 0
