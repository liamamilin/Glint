"""SQLite usage logger with pre-persist PII/secret redaction for the Glint feedback flywheel."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from models import FeedbackRequest

# Privacy redaction patterns (design.md Section IV.E: never persist secrets/tokens/phones)
REDACT_RULES: list[tuple[re.Pattern[str], str]] = [
    # API keys across common providers (sk-..., sk_live_..., pk_test_..., ghp_..., AKIA...)
    (
        re.compile(
            r"\b(?:sk-[a-zA-Z0-9_-]{16,}|(?:sk|rk|pk)_(?:live|test|proj|ant)_[a-zA-Z0-9_-]{10,}|"
            r"gh[pousr]_[a-zA-Z0-9_]{16,}|AKIA[0-9A-Z]{16})\b"
        ),
        "[REDACTED_KEY]",
    ),
    # JWT tokens (header.payload.signature)
    (
        re.compile(r"\beyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}\b"),
        "[REDACTED_JWT]",
    ),
    # Bearer tokens
    (re.compile(r"(?i)\b(Bearer\s+)[a-zA-Z0-9._\-]{8,}"), r"\1[REDACTED_TOKEN]"),
    # URL embedded userinfo credentials (https://user:pass@host)
    (re.compile(r"(https?://[^\s/:@]+:)[^\s@]+(@)"), r"\1[REDACTED]\2"),
    # Quoted English/Chinese key-value secrets (allows spaces inside quotes)
    (
        re.compile(
            r"(?i)((?:\b(?:api[_-]?key|access[_-]?token|secret|password|passwd|pwd)|密码|口令|密钥|秘钥|私钥)\s*[:=：]\s*)(['\"])[^'\"\n]+\2"
        ),
        r"\1\2[REDACTED]\2",
    ),
    # Unquoted English/Chinese key-value secrets (supports short passwords >= 1 char)
    (
        re.compile(
            r"(?i)((?:\b(?:api[_-]?key|access[_-]?token|secret|password|passwd|pwd)|密码|口令|密钥|秘钥|私钥)\s*[:=：]\s*)([^\s'\",;，。；]+)"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)"), r"\1****\2"),
]


def redact_sensitive_text(text: str | None) -> str | None:
    """Scrub API keys, bearer tokens, passwords, and mobile numbers before writing to disk."""
    if text is None:
        return None
    cleaned = text
    for pattern, repl in REDACT_RULES:
        cleaned = pattern.sub(repl, cleaned)
    return cleaned


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
    custom_intent TEXT,
    predicted_primitive TEXT,
    predicted_goal TEXT,
    predicted_clarity TEXT,
    release_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_logs_timestamp ON usage_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_logs_skill_type ON usage_logs(skill_type);
"""

MIGRATION_COLUMNS = (
    ("custom_intent", "TEXT"),
    ("predicted_primitive", "TEXT"),
    ("predicted_goal", "TEXT"),
    ("predicted_clarity", "TEXT"),
    ("release_version", "TEXT"),
)


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
            cols = {row[1] for row in conn.execute("PRAGMA table_info(usage_logs)").fetchall()}
            altered = False
            for col_name, col_type in MIGRATION_COLUMNS:
                if col_name not in cols:
                    conn.execute(f"ALTER TABLE usage_logs ADD COLUMN {col_name} {col_type}")
                    altered = True
            if altered:
                conn.commit()

    def log_feedback(self, req: FeedbackRequest) -> int:
        ts = datetime.now(timezone.utc).isoformat()
        recommended_json = json.dumps(req.recommended_ids, ensure_ascii=False)
        safe_input = redact_sensitive_text(req.input_text) or ""
        safe_custom = redact_sensitive_text(req.custom_intent)
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
                    custom_intent,
                    predicted_primitive,
                    predicted_goal,
                    predicted_clarity,
                    release_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    req.app_name,
                    req.skill_type,
                    safe_input,
                    recommended_json,
                    req.selected_id,
                    req.inject_mode,
                    req.latency_ms,
                    safe_custom,
                    req.predicted_primitive,
                    req.predicted_goal,
                    req.predicted_clarity,
                    req.release_version,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def recent_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM usage_logs").fetchone()
            return int(row[0]) if row else 0

    def get_recall_counts(self, days: int = 30) -> dict[str, int]:
        """Return active hotkey recall counts per lens_id over the past N days."""
        cutoff = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - max(1, days) * 86400.0,
            tz=timezone.utc,
        ).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT selected_id, COUNT(*)
                FROM usage_logs
                WHERE selected_id IS NOT NULL
                  AND selected_id != ''
                  AND selected_id != '__custom__'
                  AND timestamp >= ?
                GROUP BY selected_id
                """,
                (cutoff,),
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows if row[0]}
