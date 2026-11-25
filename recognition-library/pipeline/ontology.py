"""Ontology SSOT loader, enum validator, and alias normalizer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

LIB_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ONTOLOGY_PATH = LIB_ROOT / "config" / "ontology.yaml"
DEFAULT_POLICY_PATH = LIB_ROOT / "config" / "routing_policy.yaml"
DEFAULT_SOURCES_PATH = LIB_ROOT / "config" / "sources.yaml"

PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")
MALFORMED_BRACE_RE = re.compile(r"\{\{[^}]*$|^[^{]*\}\}|\{\{\s*\}\}")


@dataclass(frozen=True)
class Ontology:
    schema_version: str
    primitives: frozenset[str]
    goals: frozenset[str]
    clarities: frozenset[str]
    action_types: frozenset[str]
    inject_modes: frozenset[str]
    statuses: frozenset[str]
    aliases: dict[str, dict[str, str]]
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: Path = DEFAULT_ONTOLOGY_PATH) -> "Ontology":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        aliases_raw = data.get("aliases") or {}
        aliases = {
            section: {str(k).strip().lower(): str(v).strip().lower() for k, v in (mapping or {}).items()}
            for section, mapping in aliases_raw.items()
        }
        return cls(
            schema_version=str(data.get("schema_version", "2.0")),
            primitives=frozenset(data.get("primitives", {}).keys()),
            goals=frozenset(data.get("goals", {}).keys()),
            clarities=frozenset(data.get("clarities", {}).keys()),
            action_types=frozenset(data.get("action_types", {}).keys()),
            inject_modes=frozenset(data.get("inject_modes", ["append", "wrap"])),
            statuses=frozenset(data.get("statuses", ["active", "quarantine", "deprecated"])),
            aliases=aliases,
            raw=data,
        )

    def normalize_enum(self, section: str, value: str, allowed: frozenset[str]) -> str | None:
        token = str(value).strip().lower()
        if token in allowed:
            return token
        mapped = self.aliases.get(section, {}).get(token)
        if mapped and mapped in allowed:
            return mapped
        return None

    def normalize_primitive(self, value: str) -> str | None:
        return self.normalize_enum("primitives", value, self.primitives)

    def normalize_goal(self, value: str) -> str | None:
        return self.normalize_enum("goals", value, self.goals)

    def normalize_clarity(self, value: str) -> str | None:
        return self.normalize_enum("clarities", value, self.clarities)

    def normalize_action_type(self, value: str) -> str | None:
        return self.normalize_enum("action_types", value, self.action_types)

    def normalize_list(
        self, section: str, values: list[str], allowed: frozenset[str]
    ) -> tuple[list[str], list[str]]:
        """Normalize a list of enum values, returning (valid_unique, unknown_values)."""
        valid: list[str] = []
        unknown: list[str] = []
        seen: set[str] = set()
        for raw_val in values:
            norm = self.normalize_enum(section, raw_val, allowed)
            if norm is None:
                unknown.append(str(raw_val))
            elif norm not in seen:
                valid.append(norm)
                seen.add(norm)
        return valid, unknown


def extract_placeholders(text: str) -> list[str]:
    """Return unique {{var}} placeholder names in order of appearance."""
    seen: set[str] = set()
    result: list[str] = []
    for match in PLACEHOLDER_RE.finditer(text or ""):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def validate_placeholders(content: str, action_type: str, inject_mode: str | None) -> list[str]:
    """Check template placeholder syntax and wrap-mode {{input}} requirement."""
    errors: list[str] = []
    if MALFORMED_BRACE_RE.search(content or ""):
        errors.append("malformed_placeholder_syntax")
    if action_type == "inject_prompt" and inject_mode == "wrap":
        placeholders = extract_placeholders(content)
        if "input" not in placeholders:
            errors.append("wrap_mode_missing_input_placeholder")
    return errors


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_sources(path: Path = DEFAULT_SOURCES_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
