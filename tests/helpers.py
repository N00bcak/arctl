from __future__ import annotations

from typing import Any

from arctl.agent_backend import BackendAdapter


def fake_backend_adapter(*, certification: str = "experimental") -> BackendAdapter:
    return BackendAdapter(
        identifier="fake-v1",
        version="test-1",
        certification=certification,
        conformance_suite="fake-conformance-v1",
        capabilities=frozenset(
            {"fresh_session", "structured_output", "workspace_read", "workspace_write"}
        ),
        command=lambda agent, request: ("fake-agent", agent.name, request.output_name),
        environment=lambda credential_home, writable_home: {},
    )


def valid_task() -> dict[str, Any]:
    return {
        "schema_version": 3,
        "task_id": "demo",
        "repo": "/tmp/subject",
        "objective": "Improve the score.",
        "editable_paths": ["src/**", "tests/**"],
        "denied_paths": [".git/**", "pyproject.toml"],
        "public_checks": [["python3", "-m", "unittest"]],
        "public_probe": ["python3", "tools/probe.py"],
        "environment": {
            "sources": [
                {
                    "id": "public-environment",
                    "kind": "documentation",
                    "description": "Public environment behavior.",
                    "path": "ENVIRONMENT.md",
                },
                {
                    "id": "environment-probe",
                    "kind": "probe",
                    "description": "Environment-only probe.",
                    "command": ["python3", "-c", "print('environment')"],
                    "backed_by": ["public-environment"],
                },
            ]
        },
        "evaluator": {"repo": "/tmp/evaluator", "commit": "a" * 40},
        "strategy": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        "execution": {"model": "gpt-5.6-terra", "reasoning_effort": "medium"},
        "trials": "auto",
        "max_experiments": 30,
    }


def valid_task_v4(*, hotseat: bool = False) -> dict[str, Any]:
    value = valid_task()
    value["schema_version"] = 4
    for field in ("strategy", "planning", "execution", "reflection"):
        value.pop(field, None)
    value["environment"] = {
        "codebases": [
            {
                "id": "environment-core",
                "description": "Public environment implementation.",
                "repo": "/tmp/environment",
                "commit": "b" * 40,
                "include": ["src/**", "README.md"],
            }
        ],
        "probes": [
            {
                "id": "environment-probe",
                "description": "Policy-free environment probe.",
                "command": ["python3", "tools/probe.py"],
                "backed_by": ["environment-core"],
            }
        ],
    }
    value["method"] = {
        "profile": "serial-hotseat-v1" if hotseat else "serial-v1",
        "allow_unverified_isolation": False,
    }
    return value


def valid_task_v5(*, hotseat: bool = False) -> dict[str, Any]:
    value = valid_task_v4(hotseat=hotseat)
    value["schema_version"] = 5
    value["public_probe"] = {
        "command": value["public_probe"],
        "trial_equivalents": 3,
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
