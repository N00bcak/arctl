"""Controller-owned statistical decision rules."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from .models import Evidence


class Decision(str, Enum):
    ACCEPT = "ACCEPT"
    ARCHIVE = "ARCHIVE"
    REJECT = "REJECT"
    INVALID = "INVALID"
    PROVISIONAL = "PROVISIONAL"


FailureSource = Literal[
    "candidate",
    "champion",
    "evaluator",
    "evidence",
    "sandbox",
    "controller",
    "host",
    "stop",
    "crash",
]


def decide(evidence: Evidence) -> Decision:
    """Apply the fixed decision table to validated evidence."""
    if not evidence.hard_rules_pass or evidence.effect_estimate <= 0:
        return Decision.REJECT
    if evidence.one_sided_lower_bound <= 0:
        return Decision.ARCHIVE
    if evidence.kind == "primary" and evidence.suspect_required:
        return Decision.PROVISIONAL
    return Decision.ACCEPT


def failure_decision(source: FailureSource) -> Decision:
    """Map a failed trust-domain process to its controller-owned outcome."""
    return Decision.REJECT if source == "candidate" else Decision.INVALID
