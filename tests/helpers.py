from __future__ import annotations

from typing import Any

from arctl.agent_backend import BackendAdapter


def fake_backend_adapter(*, certification: str = "experimental") -> BackendAdapter:
    return BackendAdapter(
        identifier="fake",
        certification=certification,
        conformance_suite="fake-conformance",
        capabilities=frozenset(
            {"fresh_session", "structured_output", "workspace_read", "workspace_write"}
        ),
        command=lambda agent, request: ("fake-agent", agent.name, request.output_name),
        environment=lambda credential_home, writable_home: {},
    )


def valid_task(*, hotseat: bool = False) -> dict[str, Any]:
    value = {
        "task_id": "demo",
        "repo": "/tmp/subject",
        "objective": "Improve the score.",
        "editable_paths": ["src/**", "tests/**"],
        "denied_paths": [".git/**", "pyproject.toml"],
        "public_checks": [["python3", "-m", "unittest"]],
        "public_probe": {
            "command": ["python3", "tools/probe.py"],
            "trial_equivalents": 3,
        },
        "environment": {
            "codebases": [
                {
                    "id": "environment-core",
                    "description": "Public environment implementation.",
                    "repo": "/tmp/environment",
                    "commit": "b" * 40,
                    "include": ["src/**", "README.md"],
                },
            ],
            "probes": [
                {
                    "id": "environment-probe",
                    "description": "Policy-free environment probe.",
                    "command": ["python3", "tools/probe.py"],
                    "backed_by": ["environment-core"],
                }
            ],
        },
        "evaluator": {"repo": "/tmp/evaluator", "commit": "a" * 40},
        "method": {
            "profile": "serial-hotseat" if hotseat else "serial",
            "allow_unverified_isolation": False,
        },
        "trials": "auto",
        "max_experiments": 30,
    }
    return value


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
