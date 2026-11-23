"""Runtime configuration for Glint brain-server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("GLINT_HOST", "127.0.0.1")
    port: int = int(os.getenv("GLINT_PORT", "18765"))

    # Local inference (Ollama)
    ollama_base_url: str = os.getenv("GLINT_OLLAMA_URL", "http://127.0.0.1:11434")
    model_name: str = os.getenv("GLINT_MODEL", "qwen3.5:2b")
    keep_alive: int | str = -1
    enable_thinking: bool = _env_bool("GLINT_ENABLE_THINKING", False)
    ollama_timeout_s: float = float(os.getenv("GLINT_OLLAMA_TIMEOUT", "10.0"))
    warmup_on_startup: bool = _env_bool("GLINT_WARMUP", True)

    # Data paths
    cards_path: Path = Path(os.getenv("GLINT_CARDS_PATH", str(DATA_DIR / "cards.yaml")))
    db_path: Path = Path(os.getenv("GLINT_DB_PATH", str(DATA_DIR / "usage.db")))

    # Routing constraints
    max_input_chars: int = int(os.getenv("GLINT_MAX_INPUT_CHARS", "1200"))
    top_k_cards: int = 3


settings = Settings()
