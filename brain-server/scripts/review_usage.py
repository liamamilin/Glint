"""Quick personal review report for Glint's usage.db feedback flywheel."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "usage.db"
CARDS_PATH = ROOT / "data" / "cards.yaml"


def main() -> None:
    if not DB_PATH.exists():
        print(f"No usage database found at {DB_PATH}")
        sys.exit(0)

    cards_raw = yaml.safe_load(CARDS_PATH.read_text(encoding="utf-8")) or {}
    all_card_ids = [str(c["id"]) for c in cards_raw.get("cards", [])]

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """
        SELECT timestamp, app_name, input_text, recommended_ids, selected_id, inject_mode, latency_ms, custom_intent
        FROM usage_logs
        ORDER BY id DESC
        """
    ).fetchall()
    conn.close()

    total = len(rows)
    if total == 0:
        print("usage.db is empty. Trigger Glint a few times first!")
        return

    accepted = [r for r in rows if r[4] and r[4] != "__custom__"]
    customs = [r for r in rows if r[4] == "__custom__"]
    cancelled = [r for r in rows if r[4] is None]
    latencies = [float(r[6]) for r in rows if r[6] is not None]

    selected_counts = Counter(r[4] for r in accepted)
    recommended_counts: Counter[str] = Counter()
    for r in rows:
        try:
            for cid in json.loads(r[3]):
                recommended_counts[cid] += 1
        except Exception:
            pass

    app_counts = Counter(r[1] for r in rows)

    print("=" * 64)
    print(f"Glint Usage Review  ({total} total triggers)")
    print("=" * 64)
    print(f"  Accepted card : {len(accepted):3d} ({len(accepted) / total * 100:.1f}%)")
    print(f"  Custom intent : {len(customs):3d} ({len(customs) / total * 100:.1f}%)")
    print(f"  Dismissed/Esc : {len(cancelled):3d} ({len(cancelled) / total * 100:.1f}%)")
    if latencies:
        avg_ms = sum(latencies) / len(latencies)
        print(f"  Latency       : avg {avg_ms:.1f} ms | min {min(latencies):.1f} ms | max {max(latencies):.1f} ms")

    print("\nTop Apps:")
    for app, cnt in app_counts.most_common(6):
        print(f"  - {app:<20s} {cnt}")

    print("\nCard Performance (selected / shown):")
    for cid in sorted(all_card_ids, key=lambda c: (-selected_counts[c], -recommended_counts[c])):
        sel = selected_counts[cid]
        shown = recommended_counts[cid]
        rate = f"{sel / shown * 100:5.1f}%" if shown else "   n/a"
        print(f"  - {cid:<18s} {sel:3d} / {shown:<3d} ({rate})")

    if customs:
        print("\nRecent Custom Intents (4th option -> candidates for new cards):")
        for r in customs[:15]:
            snippet = r[2].replace("\n", " ")[:36]
            print(f"  - [{r[1]}] \"{snippet}\" -> {r[7]}")


if __name__ == "__main__":
    main()
