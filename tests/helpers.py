from __future__ import annotations

from typing import Any


def valid_task() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "task_id": "demo",
        "repo": "/tmp/subject",
        "objective": "Improve the score.",
        "editable_paths": ["src/**", "tests/**"],
        "denied_paths": [".git/**", "pyproject.toml"],
        "public_checks": [["python3", "-m", "unittest"]],
        "public_probe": ["python3", "tools/probe.py"],
        "evaluator": {"repo": "/tmp/evaluator", "commit": "a" * 40},
        "strategy": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        "execution": {"model": "gpt-5.6-terra", "reasoning_effort": "medium"},
        "trials": "auto",
        "max_experiments": 30,
    }


def valid_evidence(
    *,
    kind: str = "primary",
    estimate: float = 0.037,
    lower: float = 0.011,
    hard_rules_pass: bool = True,
    suspect_required: bool = False,
    suspect_reason: str | None = None,
    telemetry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": kind,
        "trial_count": 128,
        "hard_rules_pass": hard_rules_pass,
        "comparison": {
            "effect_estimate": estimate,
            "one_sided_lower_bound": lower,
        },
        "suspect_test": {
            "required": suspect_required,
            "reason": suspect_reason,
        },
        "telemetry": telemetry or {},
    }
