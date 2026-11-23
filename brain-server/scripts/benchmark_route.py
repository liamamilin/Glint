"""Quick end-to-end benchmark against local Ollama (qwen3.5:2b)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
from models import RouteRequest
from router import OllamaClient
from skills.cognitive_cards import CognitiveCardsSkill

CASES = [
    ("Obsidian", "我想做一个基于本地2B小模型的macOS常驻语义路由器，帮我看看这个方案行不行"),
    ("Cursor", "这段 async/await 代码在高并发下偶尔报 RuntimeError: dictionary changed size during iteration"),
    ("Mail", "帮我把这封给客户的延期说明邮件改得更自然一点，不要太像机器翻译"),
    ("Google Chrome", "在 FastAPI 和 Go 之间纠结，不知道后端服务选哪个更合适"),
    ("Notion", "我想把下季度的产品目标拆分一下，但现在思路有点乱，先从哪儿下手"),
    ("Cursor", "def merge_intervals(intervals):\n    intervals.sort()\n    res = []\n    for s, e in intervals:\n        if not res or res[-1][1] < s: res.append([s, e])\n        else: res[-1][1] = max(res[-1][1], e)\n    return res"),
]


async def main() -> None:
    client = OllamaClient(settings)
    skill = CognitiveCardsSkill(cards_path=settings.cards_path, inference=client)

    print(f"Model: {settings.model_name} | think={settings.enable_thinking} | keep_alive={settings.keep_alive}")
    print("Warming up model & KV cache...")
    await client.warmup(skill.system_prompt)
    print("-" * 80)

    latencies: list[float] = []
    for idx, (app_name, text) in enumerate(CASES, start=1):
        resp = await skill.execute(RouteRequest(app_name=app_name, input_text=text))
        latencies.append(resp.latency_ms)
        picked = ", ".join(f"{c.id}({c.dimension})" for c in resp.cards)
        print(f"[{idx}] {resp.latency_ms:6.1f} ms | fallback={resp.used_fallback} | App={app_name}")
        print(f"    Input : {text[:60]}{'...' if len(text) > 60 else ''}")
        print(f"    Cards : {picked}")

    print("-" * 80)
    avg = sum(latencies) / len(latencies)
    print(f"Average latency: {avg:.1f} ms | Min: {min(latencies):.1f} ms | Max: {max(latencies):.1f} ms")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
