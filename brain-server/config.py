"""Runtime configuration for Glint brain-server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
DATA_DIR = BASE_DIR / "data"
RECOG_LIB_DIR = REPO_ROOT / "recognition-library"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("GLINT_HOST", "127.0.0.1")
    port: int = int(os.getenv("GLINT_PORT", "18765"))

    # Local inference (Ollama embedder/fallback + in-process MPS bge-reranker-v2-m3)
    ollama_base_url: str = os.getenv("GLINT_OLLAMA_URL", "http://127.0.0.1:11434")
    model_name: str = os.getenv("GLINT_MODEL", "qwen3.5:2b")
    embed_model: str = os.getenv("GLINT_EMBED_MODEL", "bge-m3:latest")
    reranker_model: str = os.getenv("GLINT_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
    enable_reranker: bool = _env_bool("GLINT_ENABLE_RERANKER", True)
    keep_alive: int | str = -1
    enable_thinking: bool = _env_bool("GLINT_ENABLE_THINKING", False)
    ollama_timeout_s: float = float(os.getenv("GLINT_OLLAMA_TIMEOUT", "10.0"))
    warmup_on_startup: bool = _env_bool("GLINT_WARMUP", True)

    # v2.0 Recognition Library atomic release paths
    dist_dir: Path | None = Path(
        os.getenv("GLINT_DIST_DIR", str(RECOG_LIB_DIR / "dist" / "current"))
    )
    policy_path: Path = Path(
        os.getenv("GLINT_POLICY_PATH", str(RECOG_LIB_DIR / "config" / "routing_policy.yaml"))
    )
    ontology_path: Path = Path(
        os.getenv("GLINT_ONTOLOGY_PATH", str(RECOG_LIB_DIR / "config" / "ontology.yaml"))
    )

    # Legacy v0.1 fallback & usage database
    cards_path: Path = Path(os.getenv("GLINT_CARDS_PATH", str(DATA_DIR / "cards.yaml")))
    db_path: Path = Path(os.getenv("GLINT_DB_PATH", str(DATA_DIR / "usage.db")))
    seed_state_path: Path = Path(os.getenv("GLINT_SEED_STATE_PATH", str(DATA_DIR / "seed_state.json")))

    # Routing constraints
    max_input_chars: int = int(os.getenv("GLINT_MAX_INPUT_CHARS", "1200"))
    top_k_cards: int = 3


settings = Settings()
