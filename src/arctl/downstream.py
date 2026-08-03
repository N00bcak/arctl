"""Safe classification and bounded retry of transient child-process failures."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import StoppedError, TransientDownstreamError

ProgressCallback = Callable[[dict[str, Any]], None]

_CAPACITY = (
    "at capacity",
    "capacity",
    "rate limit",
    "rate_limit",
    "too many requests",
)
_NETWORK = (
    "connection error",
    "connectionerror",
    "connection reset",
    "connection refused",
    "connecttimeout",
    "readtimeout",
    "timed out",
    "timeouterror",
    "urlerror",
    "temporary failure in name resolution",
    "name or service not known",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
)
_TRANSIENT_HTTP_STATUS = re.compile(r"\b(?:408|425|429|500|502|503|504)\b")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _bounded(value: str, limit: int = 400) -> str:
    value = " ".join(_ANSI.sub("", value).split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _structured_error(stdout: Path) -> str | None:
    if not stdout.is_file():
        return None
    failed: list[str] = []
    errors: list[str] = []
    for line in stdout.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        if event.get("type") == "turn.failed":
            nested = event.get("error")
            if isinstance(nested, dict):
                message = nested.get("message")
            if isinstance(message, str):
                failed.append(message)
        elif event.get("type") == "error" and isinstance(message, str):
            errors.append(message)
    values = failed or errors
    return _bounded(values[-1]) if values else None


def _text_error(path: Path) -> str | None:
    if not path.is_file():
        return None
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    if not lines:
        return None
    traceback = next(
        (index for index, line in enumerate(lines) if line.startswith("Traceback (")),
        None,
    )
    if traceback is not None:
        for line in lines[traceback + 1 :]:
            if re.match(r"^[\w.]+(?:Error|Exception|Timeout):", line):
                return _bounded(line)
    return _bounded(lines[-1])


def primary_process_error(process: Path) -> str:
    return (
        _structured_error(process / "stdout.bin")
        or _text_error(process / "stderr.bin")
        or _text_error(process / "stdout.bin")
        or "downstream process exited unsuccessfully"
    )


def transient_process_error(
    process: Path,
    *,
    stage: str,
    codex: bool,
    fallback: str | None = None,
) -> TransientDownstreamError | None:
    detail = primary_process_error(process)
    if fallback and "timed out" in fallback.casefold():
        detail = _bounded(fallback)
    elif detail == "downstream process exited unsuccessfully" and fallback:
        detail = _bounded(fallback)
    folded = detail.casefold()
    category = None
    if codex and any(token in folded for token in _CAPACITY):
        category = "capacity"
    elif any(token in folded for token in _NETWORK) or (
        ("httperror" in folded or "http error" in folded)
        and _TRANSIENT_HTTP_STATUS.search(folded)
    ):
        category = "network"
    elif "timed out" in (fallback or "").casefold():
        category = "timeout"
    if category is None:
        return None
    return TransientDownstreamError(stage, category, detail, str(process.resolve()))


class RetryPolicy:
    """Track one invocation's consecutive transient-failure budget."""

    def __init__(
        self,
        retries: int,
        delay_seconds: float,
        *,
        progress: ProgressCallback | None,
        stop_path: Path,
    ) -> None:
        self.retries = retries
        self.delay_seconds = delay_seconds
        self.progress = progress
        self.stop_path = stop_path
        self.consecutive = 0

    def succeeded(self) -> None:
        self.consecutive = 0

    def wait(self, error: TransientDownstreamError) -> None:
        if self.consecutive >= self.retries:
            error.retries_used = self.consecutive
            error.max_retries = self.retries
            raise error
        self.consecutive += 1
        if self.progress is not None:
            self.progress(
                {
                    "event": "retry",
                    "stage": error.stage,
                    "category": error.category,
                    "attempt": self.consecutive,
                    "attempts": self.retries,
                    "delay_seconds": self.delay_seconds,
                    "detail": error.detail,
                }
            )
        deadline = time.monotonic() + self.delay_seconds
        while time.monotonic() < deadline:
            if self.stop_path.exists():
                raise StoppedError("retry stopped by request")
            time.sleep(min(0.25, deadline - time.monotonic()))
