"""Versioned, domain-separated deterministic seed derivation."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from pathlib import Path

from .errors import StateError, ValidationError

_DOMAIN = b"arctl-seed"
_PHASES = frozenset({"calibration", "primary", "suspect"})
_SUBJECTS = frozenset({"champion", "candidate", "evaluator"})


def load_setup_preflight_seeds(task_directory: Path) -> set[int]:
    path = task_directory / "setup" / "preflight.public.json"
    if not path.is_file():
        return set()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        seeds = value["seeds"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise StateError("saved setup preflight is invalid") from error
    if not isinstance(seeds, list) or any(
        isinstance(seed, bool) or not isinstance(seed, int) for seed in seeds
    ):
        raise StateError("saved setup preflight is invalid")
    return set(seeds)


def new_master_seed() -> bytes:
    return secrets.token_bytes(32)


def derive_seed(
    master: bytes,
    *,
    experiment_id: int,
    phase: str,
    subject: str,
    trial: int,
) -> int:
    if len(master) < 32:
        raise ValidationError("master seed must contain at least 256 bits")
    if isinstance(experiment_id, bool) or experiment_id < 0:
        raise ValidationError("experiment_id must be a non-negative integer")
    if phase not in _PHASES:
        raise ValidationError(f"unknown seed phase: {phase}")
    if subject not in _SUBJECTS:
        raise ValidationError(f"unknown seed subject: {subject}")
    if isinstance(trial, bool) or trial < 0:
        raise ValidationError("trial must be a non-negative integer")
    message = b"\0".join(
        (
            _DOMAIN,
            str(experiment_id).encode("ascii"),
            phase.encode("ascii"),
            subject.encode("ascii"),
            str(trial).encode("ascii"),
        )
    )
    digest = hmac.new(master, message, hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big")
