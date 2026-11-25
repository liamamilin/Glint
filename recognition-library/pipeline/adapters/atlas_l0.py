"""Thin L0 adapter for ATLAS-METHODS SkillRecord v0.1.1 JSONL files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.schema import L0RawItem, content_sha256, make_stable_id


def _format_skill_record(record: dict[str, Any]) -> tuple[str, str]:
    """Flatten a verbose SkillRecord v0.1.1 into compact procedural text for L0->L1 refinement."""
    content = record.get("content") or {}
    source = record.get("source") or {}

    title = str(content.get("title") or content.get("name") or source.get("skill_path") or "Untitled").strip()
    lines: list[str] = [
        f"标题: {title}",
        f"仓库: {source.get('repository', 'unknown')} ({source.get('skill_path', '')})",
    ]
    summary = str(content.get("summary") or "").strip()
    if summary:
        lines.append(f"摘要: {summary}")

    tasks = content.get("tasks") or []
    if tasks:
        lines.append("\n核心任务与步骤:")
        for t in tasks[:6]:
            goal = str(t.get("goal") or "").strip()
            when = str(t.get("when") or "").strip()
            lines.append(f"- 目标: {goal}" + (f" (触发: {when})" if when else ""))
            for step in (t.get("steps") or [])[:5]:
                action = str(step.get("action") or "").strip()
                if action:
                    lines.append(f"  * {action}")
            for chk in (t.get("checks") or [])[:3]:
                chk_text = str(chk.get("text") or "").strip()
                if chk_text:
                    lines.append(f"  [检查] {chk_text}")

    rules = content.get("rules") or []
    if rules:
        lines.append("\n约束与红线:")
        for r in rules[:8]:
            r_text = str(r.get("text") or "").strip()
            if r_text:
                lines.append(f"- {r_text}")

    notes = content.get("notes") or []
    if notes:
        lines.append("\n补充说明:")
        for n in notes[:4]:
            topic = str(n.get("topic") or "").strip()
            text = str(n.get("text") or "").strip()
            if text:
                lines.append(f"- {topic}: {text}" if topic else f"- {text}")

    return title, "\n".join(lines)[:6000]


def load_atlas_l0_sample(jsonl_path: Path, limit: int = 20, offset: int = 0) -> list[L0RawItem]:
    """Read up to `limit` SkillRecord items starting at `offset` from ATLAS-METHODS JSONL."""
    if not jsonl_path.exists():
        raise FileNotFoundError(f"ATLAS-METHODS JSONL not found: {jsonl_path}")

    items: list[L0RawItem] = []
    seen_idx = 0
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if seen_idx < offset:
                seen_idx += 1
                continue
            seen_idx += 1

            record = json.loads(line)
            src = record.get("source") or {}
            repo = str(src.get("repository") or "atlas")
            skill_path = str(src.get("skill_path") or f"line-{seen_idx}")
            entry_hash = str(src.get("entry_sha256") or "")
            source_ref = f"{repo}:{skill_path}"

            title, raw_text = _format_skill_record(record)
            c_hash = entry_hash or content_sha256(raw_text)
            item_id = make_stable_id("atlas", f"{repo}_{skill_path}")

            items.append(
                L0RawItem(
                    id=item_id,
                    source="atlas-methods",
                    source_ref=source_ref,
                    title=title,
                    raw_text=raw_text,
                    content_hash=c_hash,
                    metadata={
                        "repository": repo,
                        "skill_path": skill_path,
                        "commit_sha": src.get("commit_sha"),
                    },
                )
            )
            if len(items) >= limit:
                break
    return items
