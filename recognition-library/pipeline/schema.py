"""Strict Pydantic schemas for L0 -> L1 -> L2 -> dist/current pipeline."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

SLUG_RE = re.compile(r"[^a-z0-9_]+")


def make_stable_id(prefix: str, raw_key: str) -> str:
    """Create a deterministic, filesystem-safe ASCII lens/material ID."""
    cleaned = SLUG_RE.sub("_", raw_key.strip().lower()).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    if cleaned and len(cleaned) <= 48:
        base = f"{prefix}_{cleaned}" if prefix else cleaned
        return base[:64].strip("_")
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:10]
    head = cleaned[:32].strip("_") if cleaned else "item"
    return f"{prefix}_{head}_{digest}" if prefix else f"{head}_{digest}"


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# =====================================================================
# L0: Cognitive Raw Material (认知原料)
# =====================================================================

class L0RawItem(BaseModel):
    id: str
    source: str
    source_ref: str
    title: str
    raw_text: str
    content_hash: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class L0GateResult(BaseModel):
    accept: bool
    quality_score: float = Field(ge=0.0, le=1.0)
    reject_reason: str | None = None
    title: str | None = ""
    domain_hints: list[str] | None = Field(default_factory=list)
    problem_solved: str | None = ""
    when_to_use: str | None = ""
    procedural_steps: list[str] | None = Field(default_factory=list)
    anti_patterns: list[str] | None = Field(default_factory=list)
    refined_content: str | None = ""


# =====================================================================
# L1: Cognitive Material (认知素材)
# =====================================================================

class L1Material(BaseModel):
    id: str
    source: str
    source_ref: str
    title: str
    content: str
    content_hash: str
    suggested_action_type: str = "inject_prompt"
    domain_hints: list[str] = Field(default_factory=list)
    problem_solved: str = ""
    when_to_use: str = ""
    procedural_steps: list[str] = Field(default_factory=list)
    anti_patterns: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# =====================================================================
# L2: Glint Cognitive Lens (Glint 认知素材)
# =====================================================================

class LensRouting(BaseModel):
    primitive: str
    goals: list[str] = Field(min_length=1)
    clarity_fit: list[str] = Field(min_length=1)
    domains: list[str] = Field(default_factory=list)
    app_affinity: list[str] = Field(default_factory=list)
    pain_point: str = Field(min_length=4)
    trigger_utterances: list[str] = Field(min_length=1)
    anti_triggers: list[str] = Field(default_factory=list)


class WorkflowSpec(BaseModel):
    workflow_id: str
    step: int = 1
    next_lens_id: str | None = None


class LensExecution(BaseModel):
    action_type: str = "inject_prompt"
    inject_mode: Literal["append", "wrap"] | None = "append"
    content: str = Field(min_length=4)
    variables: list[str] = Field(default_factory=list)
    workflow: WorkflowSpec | None = None


class LensMeta(BaseModel):
    source: str
    source_ref: str
    source_hash: str
    status: Literal["active", "quarantine", "deprecated"] = "active"
    scope: Literal["universal", "domain_specific"] = "domain_specific"
    shelf: Literal["evergreen", "on_shelf", "off_shelf"] = "on_shelf"
    tier: Literal["S", "A", "B"] = "B"
    category: str = "general"
    subcategory: str = "problem_solving"
    collections: list[str] = Field(default_factory=list)
    base_weight: float = Field(default=1.0, ge=0.0, le=5.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    version: int = 1


class L2Lens(BaseModel):
    id: str
    title: str = Field(min_length=2)
    tagline: str = Field(min_length=2)
    routing: LensRouting
    execution: LensExecution
    meta: LensMeta

    def routing_profile_text(self) -> str:
        """Build the exact text fed to bge-m3 for semantic retrieval.

        Deliberately excludes execution.content (prompt boilerplate) so vectors
        align closely with fragmented user utterances.
        """
        parts = [
            f"标题: {self.title}",
            f"摘要: {self.tagline}",
            f"痛点: {self.routing.pain_point}",
        ]
        if self.routing.trigger_utterances:
            parts.append("触发口语: " + " | ".join(self.routing.trigger_utterances))
        if self.routing.domains:
            parts.append("领域: " + ", ".join(self.routing.domains))
        return "\n".join(parts)


# =====================================================================
# Governance: Quarantine & Overlay & Build Manifest
# =====================================================================

class QuarantineRecord(BaseModel):
    id: str
    source: str
    source_ref: str
    source_hash: str
    reasons: list[str]
    duplicate_of: str | None = None
    duplicate_score: float | None = None
    draft: dict[str, Any] = Field(default_factory=dict)


class LensOverride(BaseModel):
    title: str | None = None
    tagline: str | None = None
    status: Literal["active", "quarantine", "deprecated"] | None = None
    scope: Literal["universal", "domain_specific"] | None = None
    shelf: Literal["evergreen", "on_shelf", "off_shelf"] | None = None
    tier: Literal["S", "A", "B"] | None = None
    category: str | None = None
    subcategory: str | None = None
    collections: list[str] | None = None
    base_weight: float | None = Field(default=None, ge=0.0, le=5.0)
    primitive: str | None = None
    domains: list[str] | None = None
    app_affinity: list[str] | None = None
    add_trigger_utterances: list[str] | None = None


class OverlayFile(BaseModel):
    overrides: dict[str, LensOverride] = Field(default_factory=dict)


class BuildManifest(BaseModel):
    version: str
    schema_version: str
    built_at: str
    embedding_model: str
    embedding_dim: int
    total_l3_inventory: int = 0
    total_active_lenses: int
    total_quarantined: int
    counts_by_shelf: dict[str, int] = Field(default_factory=dict)
    counts_by_tier: dict[str, int] = Field(default_factory=dict)
    counts_by_category: dict[str, int] = Field(default_factory=dict)
    counts_by_primitive: dict[str, int]
    counts_by_source: dict[str, int]
    lenses_sha256: str
    embeddings_sha256: str | None = None
