"""SQLite usage logger for the Glint feedback data flywheel."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from models import FeedbackRequest

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS usage_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    app_name TEXT NOT NULL,
    skill_type TEXT NOT NULL,
    input_text TEXT NOT NULL,
    recommended_ids TEXT NOT NULL,
    selected_id TEXT,
    inject_mode TEXT,
    latency_ms REAL,
    custom_intent TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_logs_timestamp ON usage_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_logs_skill_type ON usage_logs(skill_type);
"""


class UsageLogger:
    """Lightweight SQLite store for positive and negative (Esc) routing feedback."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(CREATE_TABLE_SQL)
            # Safe forward migration for existing usage.db files created before custom_intent existed.
            cols = {row[1] for row in conn.execute("PRAGMA table_info(usage_logs)").fetchall()}
            if "custom_intent" not in cols:
                conn.execute("ALTER TABLE usage_logs ADD COLUMN custom_intent TEXT")
                conn.commit()

    def log_feedback(self, req: FeedbackRequest) -> int:
        ts = datetime.now(timezone.utc).isoformat()
        recommended_json = json.dumps(req.recommended_ids, ensure_ascii=False)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO usage_logs (
                    timestamp,
                    app_name,
                    skill_type,
                    input_text,
                    recommended_ids,
                    selected_id,
                    inject_mode,
                    latency_ms,
                    custom_intent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    req.app_name,
                    req.skill_type,
                    req.input_text,
                    recommended_json,
                    req.selected_id,
                    req.inject_mode,
                    req.latency_ms,
                    req.custom_intent,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def recent_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM usage_logs").fetchone()
            return int(row[0]) if row else 0
