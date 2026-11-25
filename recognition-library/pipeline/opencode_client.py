"""OpenCode Go CLI client for offline L0->L1 refinement and L1->L2 enrichment.

Modeled after the hardened invocation patterns in collect_directory_go.py:
- Always closes stdin (subprocess.DEVNULL) so OpenCode never appends fd 0 to prompts
- Enforces read-only permissions via OPENCODE_CONFIG_CONTENT
- Parses structured JSONL output events and classifies transient vs fatal errors
- Supports bounded retry on transient transport/capacity errors
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CLI = Path(os.getenv("OPENCODE_CLI", str(Path.home() / ".opencode/bin/opencode")))
DEFAULT_MODEL = os.getenv("GLINT_OPENCODE_MODEL", "opencode-go/beam-free")

TRANSIENT_RE = re.compile(
    r"upstream_stream_interrupted|connection reset by peer|ECONNRESET|ETIMEDOUT|"
    r"socket connection was closed unexpectedly|ttft_admission_refused",
    re.I,
)
FATAL_RE = re.compile(
    r"quota|usage limit|insufficient.balance|unauthorized|authentication|"
    r"invalid.*api.?key|payment",
    re.I,
)


class OpenCodeError(RuntimeError):
    """Base error for OpenCode Go invocations."""


class TransientOpenCodeError(OpenCodeError):
    """Retryable transport or temporary capacity error."""


class ContentBlockedOpenCodeError(OpenCodeError):
    """Provider content filter blocked this item (do not retry)."""


class FatalOpenCodeError(OpenCodeError):
    """Account/quota/auth error (stop the batch immediately)."""


@dataclass
class OpenCodeResponse:
    text: str
    parsed_json: dict[str, Any]
    model: str
    elapsed_s: float
    raw_events: list[dict[str, Any]]


def parse_opencode_jsonl(raw_output: str) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract text chunks, error objects, and all parsed JSONL events."""
    texts: list[str] = []
    errors: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        ev_type = event.get("type")
        if ev_type == "text":
            part = event.get("part") or {}
            chunk = part.get("text", "")
            if chunk:
                texts.append(str(chunk))
        elif ev_type == "error":
            err = event.get("error", {})
            errors.append(err if isinstance(err, dict) else {"message": str(err)})
    return texts, errors, events


def classify_provider_errors(errors: list[dict[str, Any]]) -> type[OpenCodeError] | None:
    """Classify OpenCode JSONL error events into Fatal / ContentBlocked / Transient."""
    if not errors:
        return None
    for err in errors:
        status = str(err.get("status", ""))
        msg = str(err.get("message", ""))
        err_type = str(err.get("type", ""))
        if status in {"401", "402", "403"} or (FATAL_RE.search(msg) and not TRANSIENT_RE.search(msg)):
            return FatalOpenCodeError
        if err_type == "provider.content-filter":
            return ContentBlockedOpenCodeError
    all_transient = all(
        str(e.get("status", "")) in {"502", "503", "504", "429"}
        or bool(TRANSIENT_RE.search(str(e.get("message", ""))))
        or (
            e.get("type") == "provider.invalid-output"
            and str(e.get("status", "")) == "200"
            and e.get("message") == "OpenAI Chat stream ended without finish_reason"
        )
        for e in errors
    )
    return TransientOpenCodeError if all_transient else OpenCodeError


def extract_json_object(text: str) -> dict[str, Any]:
    """Robustly extract the first valid top-level JSON object from model text."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped).strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[match.start() :])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    raise OpenCodeError(f"Model output did not contain a valid JSON object: {text[:300]!r}")


class OpenCodeGoClient:
    """Stateless, read-only JSON completion client backed by OpenCode Go CLI."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        cli_path: Path = DEFAULT_CLI,
        timeout_s: float = 180.0,
        max_retries: int = 2,
        retry_delay_s: float = 6.0,
        cwd: Path | None = None,
    ) -> None:
        self.model = model
        self.provider = model.split("/", 1)[0] if "/" in model else "opencode-go"
        self.cli_path = Path(cli_path)
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.retry_delay_s = retry_delay_s
        self.cwd = cwd or Path.cwd()

    def _readonly_config(self) -> dict[str, Any]:
        permission = {
            "*": "deny",
            "read": "deny",
            "edit": "deny",
            "shell": "deny",
            "glob": "deny",
            "grep": "deny",
            "webfetch": "deny",
            "websearch": "deny",
        }
        return {
            "model": self.model,
            "small_model": self.model,
            "snapshot": False,
            "enabled_providers": [self.provider],
            "permission": permission,
            "agent": {
                "build": {
                    "model": self.model,
                    "steps": 1,
                    "permission": permission,
                }
            },
        }

    def call_json(self, prompt: str) -> OpenCodeResponse:
        """Run one isolated prompt against OpenCode Go and return parsed JSON."""
        if not self.cli_path.exists():
            raise FatalOpenCodeError(f"OpenCode CLI not found at {self.cli_path}")

        attempt = 0
        while True:
            attempt += 1
            started = time.perf_counter()
            env = {
                **os.environ,
                "OPENCODE_CONFIG_CONTENT": json.dumps(self._readonly_config()),
                "NEXT_TELEMETRY_DISABLED": "1",
            }
            args = [
                str(self.cli_path),
                "run",
                "--standalone",
                "--format",
                "json",
                "--model",
                self.model,
                prompt,
            ]
            try:
                completed = subprocess.run(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=self.timeout_s,
                    cwd=str(self.cwd),
                    env=env,
                )
            except subprocess.TimeoutExpired as exc:
                if attempt <= self.max_retries:
                    time.sleep(self.retry_delay_s)
                    continue
                raise TransientOpenCodeError(f"OpenCode timed out after {self.timeout_s}s") from exc

            elapsed = round(time.perf_counter() - started, 2)
            combined_out = completed.stdout or ""
            texts, errors, events = parse_opencode_jsonl(combined_out)

            err_cls = classify_provider_errors(errors)
            if err_cls is not None:
                summary = "; ".join(str(e.get("message", e)) for e in errors)[:400]
                if err_cls is TransientOpenCodeError and attempt <= self.max_retries:
                    time.sleep(self.retry_delay_s)
                    continue
                raise err_cls(f"OpenCode provider error ({self.model}): {summary}")

            full_text = "".join(texts).strip()
            if completed.returncode != 0 and not full_text:
                stderr_snippet = (completed.stderr or "")[:400]
                if attempt <= self.max_retries and TRANSIENT_RE.search(stderr_snippet):
                    time.sleep(self.retry_delay_s)
                    continue
                raise OpenCodeError(
                    f"OpenCode exited with code {completed.returncode}: {stderr_snippet or combined_out[:300]}"
                )

            try:
                parsed = extract_json_object(full_text)
            except OpenCodeError:
                if attempt <= self.max_retries:
                    time.sleep(2.0)
                    continue
                raise

            return OpenCodeResponse(
                text=full_text,
                parsed_json=parsed,
                model=self.model,
                elapsed_s=elapsed,
                raw_events=events,
            )
