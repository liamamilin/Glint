"""End-to-end live smoke test for Glint brain-server v2.0 dual-path router."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
from models import RouteRequest
from router import OllamaClient
from skills.lens_router import LensRouterSkill

CASES = [
    ("Cursor", "硬核一点、去AI味、只给表格，帮我分析这个微服务架构，感觉模块耦合太重了"),
    ("Notion", "想做个独立的AI工具产品，但方向太散了完全没头绪"),
    ("Obsidian", "后端用 Rust 还是 Go，好纠结，帮我对比一下取舍"),
    ("Mail", "把这段回复客户的延期邮件改得自然一点，去掉AI套话"),
]


async def main() -> None:
    client = OllamaClient(settings)
    skill = LensRouterSkill(
        dist_dir=settings.dist_dir,  # type: ignore[arg-type]
        policy_path=settings.policy_path,
        inference=client,
    )
    print(f"Loaded {len(skill.cards)} lenses (release={skill.release_version})")
    await client.warmup(skill.system_prompt)

    for app_name, text in CASES:
        res = await skill.execute(RouteRequest(app_name=app_name, input_text=text))
        intent = res.intent
        print(f"\n=== [{app_name}] {text} ===")
        print(
            f"Intent: primitive={intent.primitive}, goal={intent.goal}, clarity={intent.clarity} "
            f"| embed={intent.used_embed} | fallback={res.used_fallback} | latency={res.latency_ms}ms"
        )
        for idx, card in enumerate(res.cards, start=1):
            exp = " [EXPLORE]" if card.is_exploration else ""
            print(
                f"  {idx}. [{card.primitive}] {card.title} ({card.source}, {card.action_type}, score={card.score}){exp}"
            )
            print(f"     preview: {card.preview}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
