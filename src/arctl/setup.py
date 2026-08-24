"""Resumable, pre-approval Python research-workspace setup."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaError

from .agent_backend import AgentSessionRequest, agent_command, agent_environment
from .commands import render_command
from .comparison_run import (
    _validate_batch,
    _validate_prepare_response,
    _validate_subject_output,
)
from .downstream import primary_process_error
from .errors import ProcessError, StateError, ValidationError
from .manifest import EvaluatorManifest
from .methods import AgentDefinition
from .models import Evidence, TaskConfig, validate_task_id
from .process import run_or_load_once
from .sandbox import (
    command_runtime_read_paths,
    marked_command,
    sandbox_command,
    sanitized_environment,
)
from .setup_protocol import EVALUATOR_ENTRYPOINT, SETUP_API_MODULE, SUBJECT_ENTRYPOINT
from .setup_conversation import (
    batch_schema,
    finalize_design,
    finalized_design_schema,
    load_decisions,
    save_batch,
    validate_batch,
)
from .storage import atomic_write_json, atomic_write_text
from .taskio import load_task

SetupCommandBuilder = Callable[[Path, Path, Path, str], Sequence[str]]
SetupProgress = Callable[[Mapping[str, Any]], None]

QUESTION_IDS = (
    "objective",
    "policy_boundary",
    "environment_boundary",
    "independent_trial",
    "outcome",
    "hidden_data",
    "hard_rules",
    "randomness",
    "telemetry",
    "runtime_budget",
    "evaluator_pattern",
)
QUESTION_GROUPS = {
    "target": ("objective", "outcome"),
    "trial_protocol": ("independent_trial", "hidden_data", "randomness"),
    "constraints": ("hard_rules", "runtime_budget"),
    "evaluator": ("telemetry", "evaluator_pattern"),
}
HUMAN_QUESTION_GROUPS = ("target", "constraints")
DISCOVERY_SCHEMA_VERSION = 5
SETUP_CONTROLLER_CONTRACT = {
    "comparison": (
        "For each experiment, freeze the current accepted champion as comparator "
        "before reserving one ordered paired batch; run each arm once on that batch."
    ),
    "seeds": (
        "Controller-reserved calibration, primary, and suspect seeds never overlap. "
        "The evaluator materializes identical paired cases for both arms without "
        "exposing illegitimate hidden seed information to the policy."
    ),
    "calibration": (
        "When trials are automatic, evaluate the initial champion once at the ladder "
        "ceiling and choose the smallest rung whose approved diagnostic and every "
        "larger rung satisfy the approved threshold; otherwise use the ceiling."
    ),
    "decision": (
        "Positive lower bound accepts; positive effect with non-positive lower bound "
        "archives; non-positive effect rejects. Only ACCEPT promotes."
    ),
    "failure": (
        "Illegal output, exceptions, timeout, resource exhaustion, malformed evidence, "
        "or evaluator failure are operational failures with no statistical score; "
        "they are never imputed as bad trial outcomes."
    ),
}
SETUP_CAPABILITIES = {
    "distinct_reserved_seeds": {
        "level": "enforced",
        "guarantee": "distinct controller-reserved 64-bit seeds across setup, calibration, primary, and suspect runs",
    },
    "paired_batches": {
        "level": "enforced",
        "guarantee": "identical ordered paired batches",
    },
    "process_timeout": {
        "level": "enforced",
        "guarantee": "a timeout for each evaluator or subject process",
    },
    "process_output_limit": {
        "level": "enforced",
        "guarantee": "a maximum captured-output size for each process",
    },
    "unscored_failures": {
        "level": "enforced",
        "guarantee": "operational failures remain unscored",
    },
    "mapped_seed_collision": {
        "level": "fail_closed",
        "guarantee": "mapped seed collisions fail the reserved batch without redrawing it",
    },
    "comparison_deadline": {
        "level": "unsupported",
        "guarantee": "whole-comparison wall-clock deadline",
    },
    "memory_limit": {
        "level": "unsupported",
        "guarantee": "memory or peak-RSS limit",
    },
    "dependency_immutability": {
        "level": "unsupported",
        "guarantee": "dependency-version immutability after approval",
    },
    "process_resource_telemetry": {
        "level": "unsupported",
        "guarantee": "process wall-time or peak-RSS telemetry",
    },
}
UNSUPPORTED_PROCESS_TELEMETRY = frozenset(
    {
        "process_wall_time_seconds",
        "advisory_process_wall_time_seconds",
        "peak_rss_bytes",
        "advisory_peak_rss_bytes",
        "process_memory_bytes",
    }
)
SETUP_BUILD_CONTRACT = {
    "schema_version": 4,
    "hooks": {
        "subject": "run_batch(public_batch) -> JSON object",
        "prepare": "prepare(PrepareContext) -> PreparedBatch",
        "calibrate": "calibrate(CalibrationContext) -> list[CalibrationAssessment]",
        "score": (
            "score(ScoreContext) -> ScoreAssessment"
        ),
    },
    "task_required": [
        "objective",
        "editable_paths",
        "denied_paths",
        "public_checks",
        "public_probe",
        "environment",
        "trials",
        "max_experiments",
    ],
    "task_controller_owned": [
        "schema_version",
        "task_id",
        "repo",
        "evaluator",
        "method",
    ],
    "python_execution": {
        "kind": "module or script",
        "target": "dotted module or repository-relative .py file",
        "arguments": "argument strings; arctl supplies interpreter and cwd",
    },
    "public_probe": ["execution", "trial_equivalents"],
    "environment": {
        "codebases": ["id", "description", "owner", "include"],
        "probes": ["id", "description", "execution", "backed_by"],
        "backed_by": "list of codebase id values; never file paths",
    },
    "manifest": {
        "root": [
            "limits",
            "schemas",
            "public",
            "trial",
            "statistics",
            "variation",
            "suspect_test",
            "calibration",
            "setup_contract",
        ],
        "limits": ["timeout_seconds", "max_output_bytes"],
        "schemas": ["public_case_json", "subject_result_json"],
        "public": ["statistic", "subject_interface", "telemetry list"],
        "telemetry_metric": [
            "description",
            "unit",
            "scope",
            "role",
            "value_type",
            "direction",
        ],
        "telemetry_wire": {
            "paired number": {"champion": "finite number", "candidate": "finite number"},
            "comparison number or boolean": {"value": "declared value type"},
            "invariant": "exactly the declared names; no null values",
        },
        "trial": ["meaning", "dependence", "seed_to_case", "subject_visible_seed"],
        "statistics": ["score", "uncertainty", "positive_effect"],
        "variation": ["known", "mitigations"],
        "suspect_test": ["trigger", "reason_codes"],
        "calibration": ["supported", "policy", "ladder", "diagnostic"],
        "calibration_diagnostic": ["name", "units", "maximum"],
        "setup_contract": [
            "environment_adapter",
            "outcome",
            "trial",
            "hard_rules",
            "runtime_limits",
        ],
    },
    "controller_owned": [
        "all commands and placeholders",
        "_arctl/subject.py",
        "_arctl/evaluator.py",
        "evaluator.manifest.json",
    ],
}
_SETUP_TEMPLATE = """# ARCTL setup

<!-- Fill what you know. arctl will inspect the repository and ask only about
material gaps or conflicts. Delete no headings; empty sections are allowed. -->

## Goal and primary outcome

## Policy boundary

## Environment boundary

## Trial protocol

<!-- State any known episode horizon, compute budget, or dependence. The setup
specialist will inspect the environment and recommend sampling and calibration. -->

## Hidden information

## Hard constraints

## Telemetry

## Runtime budget

<!-- Give the timeout scope (per episode, arm batch, or whole comparison) and
only resource limits the intended runtime can actually enforce. -->

## Evaluator and success criterion

<!-- State how cautious promotion should be and the cost of false promotion or
missed improvements. The setup specialist will recommend the statistical method. -->
"""
_PLACEHOLDERS = {
    "SETUP_SUBJECT_COMMIT",
    "SETUP_ENVIRONMENT_COMMIT",
    "SETUP_EVALUATOR_COMMIT",
}


def _git(repo: Path, *arguments: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise StateError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout.strip()


def _clean(repo: Path) -> bool:
    return not _git(repo, "status", "--porcelain", "--untracked-files=all")


def _clean_except_brief(repo: Path) -> bool:
    lines = _git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    return all(line[3:] == "ARCTL_SETUP.md" for line in lines)


def _commit(
    repo: Path,
    message: str,
    paths: Sequence[str] | None = None,
    reviewed_root: Path | None = None,
) -> str:
    if paths is None:
        dirty = bool(_git(repo, "status", "--porcelain", "--untracked-files=all"))
    else:
        if _git(repo, "diff", "--cached", "--name-only", "-z"):
            raise StateError(f"setup acceptance requires an empty Git index: {repo}")
        dirty = bool(paths)
    if dirty:
        if paths is None:
            _git(repo, "add", "--all")
        else:
            _git(repo, "add", "--all", "--", *paths)
            staged = tuple(
                item
                for item in _git(repo, "diff", "--cached", "--name-only", "-z").split("\0")
                if item
            )
            if set(staged) != set(paths):
                raise StateError("setup staged paths differ from the reviewed ownership list")
            if reviewed_root is None:
                raise StateError("setup commit is missing its reviewed source tree")
            for path in paths:
                expected = _git(repo, "hash-object", str(reviewed_root / path))
                actual = _git(repo, "rev-parse", f":{path}")
                if actual != expected:
                    raise StateError(
                        f"setup staged content differs from the reviewed file: {path}"
                    )
        _git(
            repo,
            "-c",
            "user.name=arctl setup",
            "-c",
            "user.email=setup@arctl.invalid",
            "commit",
            "-qm",
            message,
        )
    elif not _git(repo, "rev-parse", "--verify", "HEAD", check=False):
        _git(
            repo,
            "-c",
            "user.name=arctl setup",
            "-c",
            "user.email=setup@arctl.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            message,
        )
    return _git(repo, "rev-parse", "HEAD")


def _schema(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": dict(properties),
    }


def discovery_schema() -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    citation = _schema({"path": text, "location": text, "finding": text})
    question = _schema(
        {
            "id": {"type": "string", "enum": list(QUESTION_IDS)},
            "proposed_answer": text,
            "citations": {"type": "array", "items": citation},
            "source": {
                "type": "string",
                "enum": ["setup brief", "repository", "proposal"],
            },
        }
    )
    clarification = _schema(
        {
            "id": {"type": "string", "enum": list(HUMAN_QUESTION_GROUPS)},
            "prompt": text,
            "why": text,
            "proposed_answer": {"type": ["string", "null"]},
            "affected_fields": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": list(QUESTION_IDS)},
            },
        }
    )
    downgrade = _schema(
        {
            "capability_id": {
                "type": "string",
                "enum": list(SETUP_CAPABILITIES),
            },
        }
    )
    return _schema(
        {
            "schema_version": {"type": "integer", "const": DISCOVERY_SCHEMA_VERSION},
            "brief_sha256": text,
            "summary": text,
            "fields": {
                "type": "array",
                "minItems": len(QUESTION_IDS),
                "maxItems": len(QUESTION_IDS),
                "items": question,
            },
            "open_questions": {
                "type": "array",
                "maxItems": len(HUMAN_QUESTION_GROUPS),
                "items": clarification,
            },
            "capability_downgrades": {"type": "array", "items": downgrade},
        }
    )


def _brief(setup: Mapping[str, Any]) -> tuple[Path, str, str]:
    workspace = Path(setup["workspace"])
    path = workspace / "ARCTL_SETUP.md"
    return path, "", hashlib.sha256(b"").hexdigest()


def setup_presentation(discovery: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable human/JSON view, including discovery-v1 consolidation."""
    if discovery.get("schema_version") in {2, 3, 4, 5}:
        downgrades = []
        for item in discovery.get("capability_downgrades", []):
            if "capability_id" not in item:
                downgrades.append(item)
                continue
            capability = SETUP_CAPABILITIES[item["capability_id"]]
            downgrades.append(
                {
                    "requested": capability["guarantee"],
                    "supported_equivalent": (
                        "fail the reserved batch without replacement"
                        if capability["level"] == "fail_closed"
                        else "document as an unenforced requirement"
                    ),
                    "consequence": (
                        "the controller will not silently redraw or claim enforcement"
                    ),
                }
            )
        return {
            "proposal": list(discovery["fields"]),
            "open_questions": list(discovery["open_questions"]),
            "capability_downgrades": downgrades,
        }
    fields = []
    unresolved: set[str] = set()
    markers = ("must confirm", "humans must", "should confirm", "need confirmation")
    for item in discovery.get("questions", []):
        fields.append({**item, "source": "repository"})
        if any(marker in item["proposed_answer"].lower() for marker in markers):
            unresolved.add(item["id"])
    questions = []
    by_id = {item["id"]: item for item in fields}
    for group, members in QUESTION_GROUPS.items():
        affected = [name for name in members if name in unresolved]
        if not affected:
            continue
        first = by_id[affected[0]]
        questions.append(
            {
                "id": group,
                "prompt": f"Confirm the {group.replace('_', ' ')}.",
                "why": "The earlier discovery marked these related details unresolved.",
                "proposed_answer": first["proposed_answer"],
                "affected_fields": affected,
            }
        )
    return {"proposal": fields, "open_questions": questions, "capability_downgrades": []}


def brief_changed(setup: Mapping[str, Any], discovery: Mapping[str, Any]) -> bool:
    return discovery.get("schema_version") not in {4, 5} or discovery.get(
        "brief_sha256"
    ) != _brief(setup)[2]


def build_schema() -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    strings = {"type": "array", "items": text}
    command = _schema(
        {
            "kind": {"type": "string", "enum": ["module", "script"]},
            "target": text,
            "arguments": {"type": "array", "items": {"type": "string"}},
        }
    )
    file_record = _schema({"path": text, "content": {"type": "string"}})
    files = {"type": "array", "items": file_record}
    task = _schema(
        {
            "objective": text,
            "editable_paths": strings,
            "denied_paths": strings,
            "public_checks": {"type": "array", "items": command},
            "public_probe": _schema(
                {
                    "execution": command,
                    "trial_equivalents": {"type": "integer", "minimum": 1},
                }
            ),
            "environment": _schema(
                {
                    "codebases": {
                        "type": "array",
                        "minItems": 1,
                        "items": _schema(
                            {
                                "id": text,
                                "description": text,
                                "owner": {
                                    "type": "string",
                                    "enum": ["subject", "environment"],
                                },
                                "include": strings,
                            }
                        ),
                    },
                    "probes": {
                        "type": "array",
                        "items": _schema(
                            {
                                "id": text,
                                "description": text,
                                "execution": command,
                                "backed_by": strings,
                            }
                        ),
                    },
                }
            ),
            "trials": {
                "anyOf": [
                    {"type": "integer", "minimum": 1},
                    {"type": "string", "enum": ["auto"]},
                ]
            },
            "max_experiments": {
                "anyOf": [
                    {"type": "integer", "minimum": 1},
                    {"type": "string", "enum": ["unlimited"]},
                ]
            },
        }
    )
    roles = {
        "type": "string",
        "enum": ["outcome", "mechanism", "safety", "implementation", "uncertainty"],
    }
    directions = {"type": "string", "enum": ["higher", "lower", "contextual"]}
    telemetry_common = {"name": text, "description": text, "unit": text, "role": roles}
    telemetry = {
        "anyOf": [
            _schema(
                {
                    **telemetry_common,
                    "scope": {"type": "string", "enum": ["paired", "comparison"]},
                    "value_type": {"type": "string", "const": "number"},
                    "direction": directions,
                }
            ),
            _schema(
                {
                    **telemetry_common,
                    "scope": {"type": "string", "const": "comparison"},
                    "value_type": {"type": "string", "const": "boolean"},
                    "direction": {"type": "string", "const": "contextual"},
                }
            ),
        ]
    }
    diagnostic = _schema(
        {
            "name": text,
            "units": text,
            "maximum": {"type": "number", "minimum": 0},
        }
    )
    setup_contract = _schema(
        {
            "environment_adapter": _schema(
                {
                    "entrypoint": text,
                    "interface": text,
                }
            ),
            "outcome": _schema(
                {
                    "direction": {"type": "string", "enum": ["higher", "lower"]},
                    "unit": text,
                    "aggregation": text,
                    "extraction": text,
                }
            ),
            "trial": _schema(
                {
                    "termination": text,
                    "horizon_unit": text,
                }
            ),
            "hard_rules": strings,
            "runtime_limits": {"type": "array", "minItems": 1, "items": text},
        }
    )
    manifest = _schema(
        {
            "limits": _schema(
                {
                    "timeout_seconds": {"type": "integer", "minimum": 1},
                    "max_output_bytes": {"type": "integer", "minimum": 1},
                }
            ),
            "schemas": _schema(
                {
                    "public_case_json": text,
                    "subject_result_json": text,
                }
            ),
            "public": _schema(
                {
                    "statistic": text,
                    "subject_interface": text,
                    "telemetry": {"type": "array", "items": telemetry},
                }
            ),
            "trial": _schema(
                {
                    "meaning": text,
                    "dependence": text,
                    "seed_to_case": text,
                    "subject_visible_seed": {"type": "boolean"},
                }
            ),
            "statistics": _schema(
                {"score": text, "uncertainty": text, "positive_effect": text}
            ),
            "variation": _schema({"known": text, "mitigations": strings}),
            "suspect_test": {
                "anyOf": [
                    _schema(
                        {
                            "trigger": {"type": "null"},
                            "reason_codes": {
                                "type": "array",
                                "maxItems": 0,
                                "items": text,
                            },
                        }
                    ),
                    _schema(
                        {
                            "trigger": text,
                            "reason_codes": {
                                "type": "array", "minItems": 1, "items": text
                            },
                        }
                    ),
                ]
            },
            "calibration": {
                "anyOf": [
                    _schema(
                        {
                            "supported": {"type": "boolean", "const": True},
                            "policy": text,
                            "ladder": {
                                "type": "array", "minItems": 1,
                                "items": {"type": "integer", "minimum": 1},
                            },
                            "diagnostic": diagnostic,
                        }
                    ),
                    _schema(
                        {
                            "supported": {"type": "boolean", "const": False},
                            "policy": {"type": "null"},
                            "ladder": {"type": "null"},
                            "diagnostic": {"type": "null"},
                        }
                    ),
                ]
            },
            "setup_contract": setup_contract,
        }
    )
    return _schema(
        {
            "schema_version": {"type": "integer", "const": 4},
            "summary": text,
            "dependencies": {"type": "array", "items": text},
            "subject_hook": {"type": "string", "minLength": 1},
            "evaluator_hook": {"type": "string", "minLength": 1},
            "evaluator_test": {"type": "string", "minLength": 1},
            "subject_files": files,
            "environment_files": files,
            "evaluator_files": files,
            "task": task,
            "evaluator": manifest,
        }
    )


def review_schema() -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    citation = _schema({"path": text, "location": text, "finding": text})
    coverage = {
        area: _schema(
            {
                "status": {"type": "string", "enum": ["pass", "fail", "not_applicable"]},
                "summary": text,
                "evidence": {"type": "array", "items": citation},
            }
        )
        for area in (
            "intent_fidelity",
            "grounding",
            "editable_boundary",
            "dependencies",
            "trial_independence",
            "scoring_statistics",
            "seed_handling",
            "runtime_behavior",
        )
    }
    return _schema(
        {
            "schema_version": {"type": "integer", "const": 2},
            "summary": text,
            "coverage": _schema(coverage),
            "findings": {
                "type": "array",
                "items": _schema(
                    {
                        "code": text,
                        "location": text,
                        "message": text,
                    }
                ),
            },
        }
    )


def _validate_review_evidence(
    review: Mapping[str, Any], *, roots: Sequence[Path]
) -> None:
    failed = False
    for area, result in review["coverage"].items():
        status = result["status"]
        evidence = result["evidence"]
        if status == "fail":
            failed = True
        if status != "not_applicable" and not evidence:
            raise ValidationError(f"setup review area {area} requires evidence")
        for citation in evidence:
            raw = Path(citation["path"])
            candidates = [raw] if raw.is_absolute() else [root / raw for root in roots]
            existing = next(
                (path for path in candidates if path.is_file() and not path.is_symlink()),
                None,
            )
            if existing is None:
                raise ValidationError(
                    f"setup review cites a missing reviewed path: {citation['path']}"
                )
            matched = re.fullmatch(
                r"lines?\s+(\d+)(?:-(\d+))?",
                citation["location"].strip(),
                flags=re.IGNORECASE,
            )
            if matched is None:
                raise ValidationError(
                    f"setup review citation has an invalid location: {citation['location']}"
                )
            start = int(matched.group(1))
            end = int(matched.group(2) or start)
            count = len(existing.read_text(encoding="utf-8", errors="replace").splitlines())
            if start < 1 or end < start or end > max(count, 1):
                raise ValidationError(
                    f"setup review citation is outside {citation['path']}"
                )
    if failed and not review["findings"]:
        raise ValidationError("failed setup review coverage requires a finding")
    if review["findings"] and not failed:
        raise ValidationError("setup review findings require failed coverage")


def _dependency_uses_special_source(requirement: str) -> bool:
    lowered = requirement.lower()
    return (
        " @ " in requirement
        or "git+" in lowered
        or "http://" in lowered
        or "https://" in lowered
        or "file:" in lowered
        or requirement.startswith(("/", "./", "../"))
    )


def _load_authorized_design(directory: Path) -> dict[str, Any]:
    design_path = directory / "setup" / "authorized-design.public.json"
    authorization_path = directory / "setup" / "authorization.public.json"
    try:
        design = json.loads(design_path.read_text(encoding="utf-8"))
        authorization = json.loads(authorization_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("setup has no valid authorized design") from error
    try:
        Draft202012Validator(finalized_design_schema()).validate(design)
    except JsonSchemaError as error:
        raise StateError("authorized setup design is invalid") from error
    digest = hashlib.sha256(
        json.dumps(design, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        not isinstance(authorization, Mapping)
        or set(authorization)
        != {"schema_version", "design_sha256", "decision_revision", "authorized"}
        or authorization.get("schema_version") != 1
        or authorization.get("authorized") is not True
        or authorization.get("design_sha256") != digest
    ):
        raise StateError("authorized setup design changed after authorization")
    decisions = load_decisions(directory)
    if (
        authorization.get("decision_revision") != design.get("decision_revision")
        or decisions.get("revision") != design.get("decision_revision")
    ):
        raise StateError("authorized setup decisions changed after authorization")
    controller_digest = hashlib.sha256(
        json.dumps(SETUP_CONTROLLER_CONTRACT, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if design.get("controller_contract", {}).get("sha256") != controller_digest:
        raise StateError("controller setup contract changed after design authorization")
    return design


def _validate_dependency_plan(
    dependencies: Sequence[str],
    *,
    directory: Path,
) -> None:
    if any(not isinstance(item, str) or not item.strip() for item in dependencies):
        raise ValidationError("setup dependencies must be non-empty requirement strings")
    design_path = directory / "setup" / "authorized-design.public.json"
    if not design_path.is_file():
        if dependencies:
            raise ValidationError("setup dependencies lack an authorized design")
        return
    try:
        design = _load_authorized_design(directory)
    except StateError as error:
        raise ValidationError(str(error)) from error
    declared = {
        item["requirement"]: item for item in design.get("direct_dependencies", [])
    }
    unexpected = sorted(set(dependencies) - set(declared))
    if unexpected:
        setup_path = directory / "setup.json"
        setup = json.loads(setup_path.read_text(encoding="utf-8"))
        setup["state"] = "DISCOVERY_REQUIRED"
        setup["late_dependencies"] = unexpected
        atomic_write_json(setup_path, setup)
        atomic_write_json(
            directory / "setup" / "late-dependencies.public.json",
            {"schema_version": 1, "requirements": unexpected},
        )
        raise StateError(
            "build discovered direct dependencies that require a new explicit decision: "
            + ", ".join(unexpected)
        )
    decisions = {
        item["id"]
        for item in load_decisions(directory).get("decisions", [])
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    for requirement in dependencies:
        dependency = declared[requirement]
        decision = dependency.get("authorization_decision")
        if _dependency_uses_special_source(requirement) and decision not in decisions:
            raise ValidationError(
                f"dependency {requirement!r} uses a special source without an explicit decision"
            )


def _agent_failure_detail(stderr: str, stdout: str) -> str:
    if stderr:
        return stderr
    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "error" or not isinstance(event.get("message"), str):
            continue
        message = event["message"]
        try:
            nested = json.loads(message)
            inner = nested.get("error")
            if isinstance(inner, Mapping) and isinstance(inner.get("message"), str):
                return inner["message"]
        except json.JSONDecodeError:
            pass
        return message
    return stdout


def _agent_run(
    *,
    root: Path,
    worktree: Path,
    schema_value: Mapping[str, Any],
    output_name: str,
    prompt: str,
    writable_worktree: bool,
    read_paths: tuple[Path, ...] = (),
    command_builder: SetupCommandBuilder | None = None,
    offline: bool = False,
    validate_output: bool = True,
) -> dict[str, Any]:
    scratch = root / "output"
    scratch.mkdir(parents=True, exist_ok=True)
    schema = root / "output.schema.json"
    atomic_write_json(schema, schema_value)
    if command_builder is None:
        agent = AgentDefinition(
            "setup-default", "codex-cli-v1", "gpt-5.6-sol", "medium"
        )
        command = agent_command(
            agent,
            AgentSessionRequest(
                worktree=worktree,
                scratch=scratch,
                output_schema=schema,
                prompt=prompt,
                output_name=output_name,
                writable_worktree=writable_worktree,
                read_paths=read_paths,
                network_enabled=False,
            ),
        )
        environment = agent_environment(
            agent,
            credential_home=Path(
                os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
            ),
            writable_home=scratch,
        )
    else:
        command = command_builder(worktree, scratch, schema, prompt)
        environment = None
    result = run_or_load_once(
        root / "process",
        command,
        timeout_seconds=3600,
        max_output_bytes=4_000_000,
        cwd=worktree,
        env=environment,
        stdin_path=root / "prompt.public.txt" if command_builder is None else None,
    )
    if result["return_code"] != 0:
        stderr_path = root / "process" / "stderr.bin"
        stdout_path = root / "process" / "stdout.bin"
        stderr = (
            stderr_path.read_text(encoding="utf-8", errors="replace").strip()
            if stderr_path.is_file()
            else ""
        )
        stdout = (
            stdout_path.read_text(encoding="utf-8", errors="replace").strip()
            if stdout_path.is_file()
            else ""
        )
        detail = _agent_failure_detail(stderr, stdout)
        suffix = f": {detail}" if detail else ""
        raise StateError(f"setup agent failed{suffix}; inspect {root / 'process'}")
    try:
        value = json.loads((scratch / output_name).read_text(encoding="utf-8"))
        if validate_output:
            Draft202012Validator(schema_value).validate(value)
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"setup agent wrote invalid {output_name}: {error}") from error
    except JsonSchemaError as error:
        raise StateError(
            f"setup agent wrote invalid {output_name}: {error.message}"
        ) from error
    if not isinstance(value, dict):
        raise StateError(f"setup agent wrote invalid {output_name}")
    return value


def initialize_setup(
    *,
    data_root: Path,
    workspace: Path,
    source_repo: Path,
    task_id: str,
) -> dict[str, Any]:
    validate_task_id(task_id)
    workspace = workspace.resolve()
    source_repo = source_repo.resolve()
    data_root = data_root.resolve()
    if _git(source_repo, "rev-parse", "--is-inside-work-tree", check=False) != "true":
        raise StateError(f"target is not a Git worktree: {source_repo}")
    if source_repo == workspace or source_repo in workspace.parents:
        raise StateError("workspace must not be inside the source repository")
    if source_repo == data_root or source_repo in data_root.parents:
        raise StateError("setup data must not be stored inside the source repository")
    task_directory = data_root / "tasks" / task_id
    if task_directory.exists():
        raise StateError(f"task already exists: {task_id}")
    if workspace.exists():
        raise StateError(f"workspace already exists: {workspace}")
    if not _clean(source_repo):
        raise StateError("setup requires a clean source Git worktree")
    source_commit = _git(source_repo, "rev-parse", "HEAD")
    summary_output = workspace / "ARCTL_SETUP.md"
    if summary_output.exists():
        raise StateError(
            f"guided setup summary output already exists and was not changed: {summary_output}"
        )
    workspace.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{workspace.name}.init-", dir=workspace.parent)
    )
    try:
        completed = subprocess.run(
            ["git", "clone", "-q", "--no-hardlinks", str(source_repo), str(temporary / "subject")],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            raise StateError(
                "failed to ingest source repository: "
                + (completed.stderr.strip() or completed.stdout.strip())
            )
        subject = temporary / "subject"
        if _git(subject, "rev-parse", "HEAD") != source_commit:
            raise StateError("ingested subject does not match the source HEAD")
        evaluator = temporary / "evaluator"
        environment = temporary / "environment"
        evaluator.mkdir()
        environment.mkdir()
        _git(evaluator, "init", "-q")
        _git(environment, "init", "-q")
        temporary.rename(workspace)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    subject = (workspace / "subject").resolve()
    evaluator = (workspace / "evaluator").resolve()
    environment = (workspace / "environment").resolve()
    task_directory.mkdir(parents=True)
    record = {
        "schema_version": 2,
        "setup_contract": "conversation-v2",
        "task_id": task_id,
        "workspace": str(workspace),
        "data_root": str(data_root.resolve()),
        "subject": str(subject),
        "subject_base": source_commit,
        "source_repo": str(source_repo),
        "source_commit": source_commit,
        "environment": str(environment.resolve()),
        "evaluator": str(evaluator.resolve()),
        "state": "DISCOVERY_REQUIRED",
    }
    atomic_write_json(task_directory / "setup.json", record)
    atomic_write_text(
        workspace / "arctl.workspace.yaml",
        "schema_version: 1\n"
        f"task_id: {json.dumps(task_id)}\n"
        f"data_root: {json.dumps(str(data_root.resolve()))}\n"
        f"source_repo: {json.dumps(str(source_repo))}\n"
        f"source_commit: {json.dumps(source_commit)}\n"
        f"subject: {json.dumps(str(subject))}\n"
        f"environment: {json.dumps(str(environment.resolve()))}\n"
        f"evaluator: {json.dumps(str(evaluator.resolve()))}\n",
    )
    return record


def load_setup(data_root: Path, task_id: str | None) -> tuple[Path, dict[str, Any]]:
    tasks = data_root / "tasks"
    if task_id is None:
        matches = sorted(tasks.glob("*/setup.json")) if tasks.is_dir() else []
        if len(matches) != 1:
            raise StateError("setup task is missing or ambiguous; specify TASK")
        path = matches[0]
    else:
        validate_task_id(task_id)
        path = tasks / task_id / "setup.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("setup state is missing or invalid") from error
    if not isinstance(value, dict) or value.get("schema_version") not in {1, 2}:
        raise StateError("setup state is missing or invalid")
    return path.parent, value


def _save_setup(directory: Path, value: dict[str, Any]) -> None:
    atomic_write_json(directory / "setup.json", value)


def _invalidate_pending_build(
    directory: Path, setup: dict[str, Any], finding: str
) -> None:
    setup.pop("pending_build", None)
    setup["prior_build_findings"] = [finding]
    _save_setup(directory, setup)


def _upgrade_discovery(value: Mapping[str, Any]) -> dict[str, Any]:
    """Translate the immediately preceding discovery contract without guessing intent."""
    upgraded = dict(value)
    if upgraded.get("schema_version") != 4:
        return upgraded
    capability_ids = []
    for item in upgraded.get("capability_downgrades", []):
        requested = str(item.get("requested", "")).lower()
        matches = []
        for identifier, capability in SETUP_CAPABILITIES.items():
            words = capability["guarantee"].lower().replace("-", " ").split()
            if sum(word in requested for word in words if len(word) > 4) >= 2:
                matches.append(identifier)
        if not matches:
            if "memory" in requested or "rss" in requested:
                matches.append("memory_limit")
            if "whole" in requested or "overall" in requested or "comparison" in requested:
                matches.append("comparison_deadline")
        capability_ids.extend(matches)
    upgraded["schema_version"] = DISCOVERY_SCHEMA_VERSION
    upgraded["capability_downgrades"] = [
        {"capability_id": identifier} for identifier in dict.fromkeys(capability_ids)
    ]
    return upgraded


def discover_setup(
    directory: Path,
    setup: dict[str, Any],
    *,
    command_builder: SetupCommandBuilder | None = None,
    offline: bool = False,
    progress: SetupProgress | None = None,
) -> dict[str, Any]:
    subject = Path(setup["subject"])
    brief_path, brief_text, brief_hash = _brief(setup)
    prompt = (
        "Analyze this public Python repository as a prospective arctl research "
        "subject. Do not edit files or invent private evaluation data. The human "
        "ARCTL_SETUP.md below is authoritative intent; use repository evidence to "
        "complete it and identify only material missing, ambiguous, contradictory, "
        "or infeasible human goals or constraints. Act as the statistical and "
        "experimental-design specialist: inspect the environment implementation for "
        "score-affecting variation, then recommend the sampling frame, seed derivation, "
        "calibration diagnostic, uncertainty construction, finite computation choices, "
        "and reproducible evaluator RNG handling. Return exactly one canonical field for every "
        "required id. A field source is 'setup brief' when explicitly supplied, "
        "'repository' when directly derived, and 'proposal' otherwise. Return at "
        "most one non-overlapping clarification in each allowed human group: target "
        "and constraints. Never ask the human for a sampling distribution, seed/RNG "
        "mechanism, independence analysis, trial ladder, calibration diagnostic, "
        "confidence or bootstrap construction, resample count, telemetry schema, or "
        "other specialist implementation detail. Resolve those from the research goal, "
        "repository, conventional rigor, and stated resource budget; explain the choice "
        "in the proposal. If evidence is incomplete, recommend a conservative design "
        "and identify the assumption instead of delegating it. Ask the human only for "
        "an unresolved objective, outcome preference, risk tolerance, policy constraint, "
        "or resource ceiling. Never ask the human to redefine a controller-owned rule. Do not "
        "describe a process failure as a failed episode or assign it a score. Do not "
        "ask for a redundant negative-mean or tie rule when the controller decision "
        "mapping already covers it. Dependency installation is derived setup work, "
        "not a human question, unless the required dependency source is ambiguous. "
        "Policy and environment facts should normally be shown, not asked. Every "
        "field needs repository citations when available. Compare requested operational "
        "guarantees with controller_capabilities. Capability levels and consequences are "
        "controller facts: return only the matching capability_id for every requested "
        "fail-closed or unsupported guarantee. Ask one constraints clarification when "
        "human acceptance of those limits is needed; never invent an equivalent or claim "
        "enforcement. "
        "Return only JSON.\n\n"
        + json.dumps(
            {
                "brief_path": str(brief_path),
                "brief_sha256": brief_hash,
                "brief": brief_text,
                "required_ids": QUESTION_IDS,
                "allowed_human_groups": HUMAN_QUESTION_GROUPS,
                "controller_owned_contract": SETUP_CONTROLLER_CONTRACT,
                "controller_capabilities": SETUP_CAPABILITIES,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    attempts = directory / "setup" / "discovery" / "attempts"
    attempt = 1 + len(tuple(attempts.glob("*"))) if attempts.is_dir() else 1
    if progress is not None:
        progress({"stage": "brief + repository discovery", "status": "started"})
    recoverable = directory / "setup" / "discovery.pending.public.json"
    if not recoverable.is_file() and attempts.is_dir():
        completed = sorted(attempts.glob("*/output/discovery.public.json"))
        recoverable = completed[-1] if completed else recoverable
    try:
        if recoverable.is_file():
            candidate = json.loads(recoverable.read_text(encoding="utf-8"))
            value = _upgrade_discovery(candidate)
            Draft202012Validator(discovery_schema()).validate(value)
        else:
            value = _agent_run(
                root=attempts / f"{attempt:04d}",
                worktree=subject,
                schema_value=discovery_schema(),
                output_name="discovery.public.json",
                prompt=prompt,
                writable_worktree=False,
                command_builder=command_builder,
                offline=offline,
            )
    except Exception:
        if progress is not None:
            progress({"stage": "brief + repository discovery", "status": "failed"})
        raise
    # Preserve completed agent work before applying arctl's semantic checks. A
    # corrected controller may then recover it without another paid agent run.
    atomic_write_json(directory / "setup" / "discovery.pending.public.json", value)
    ids = [item["id"] for item in value["fields"]]
    if sorted(ids) != sorted(QUESTION_IDS) or len(ids) != len(set(ids)):
        raise StateError("setup discovery did not describe every required field once")
    if value["brief_sha256"] != brief_hash:
        raise StateError("setup discovery returned the wrong brief hash")
    seen_fields: set[str] = set()
    seen_groups: set[str] = set()
    for question in value["open_questions"]:
        group = question["id"]
        affected = set(question["affected_fields"])
        if group in seen_groups or affected & seen_fields:
            raise StateError("setup discovery produced overlapping clarifications")
        seen_groups.add(group)
        seen_fields.update(affected)
    unknown_capabilities = [
        item["capability_id"]
        for item in value["capability_downgrades"]
        if item["capability_id"] not in SETUP_CAPABILITIES
        or SETUP_CAPABILITIES[item["capability_id"]]["level"] == "enforced"
    ]
    if unknown_capabilities:
        raise StateError(
            "setup discovery requested invalid capability downgrades: "
            + ", ".join(sorted(unknown_capabilities))
        )
    downgrade_questions = [
        question for question in value["open_questions"] if question["id"] == "constraints"
    ]
    if value["capability_downgrades"] and len(downgrade_questions) != 1:
        raise StateError(
            "setup discovery must request one constraints confirmation for capability downgrades"
        )
    atomic_write_json(directory / "setup" / "discovery.public.json", value)
    (directory / "setup" / "discovery.pending.public.json").unlink(missing_ok=True)
    if progress is not None:
        progress({"stage": "brief + repository discovery", "status": "completed"})
    setup["state"] = "ANSWERS_REQUIRED"
    _save_setup(directory, setup)
    return value


def discover_setup_batch(
    directory: Path,
    setup: dict[str, Any],
    *,
    command_builder: SetupCommandBuilder | None = None,
    offline: bool = False,
    progress: SetupProgress | None = None,
) -> dict[str, Any]:
    """Inspect public repository state and return at most three material choices."""
    subject = Path(setup["subject"])
    decisions = load_decisions(directory)
    revision = decisions["revision"] + 1
    prompt = (
        "Inspect this public Python repository and continue a guided arctl setup. "
        "Do not edit files. Ask only consequential human-owned decisions. Return at "
        "most three related questions in this batch, each with two to four materially "
        "different grounded options, a recommendation, a concise consequence, and exact "
        "repository line citations. The objective, primary outcome, and policy/editable "
        "boundary must be explicit human decisions; ask them if they are not already in "
        "confirmed_decisions. Other implementation details should be derived when the "
        "repository and confirmed choices make them unambiguous. Never accept silence as "
        "confirmation and never combine several fields into one prose answer. Cite the "
        "controller failure rule for any late_dependency_requirements and ask an "
        "explicit allow-or-reject question for each new direct dependency before returning "
        "another design. "
        "controller citation only for a controller-owned invariant. When no material "
        "question remains, return no questions and one complete typed design. Specify exact "
        "editable paths, one canonical environment adapter, an outcome statistic with direction, "
        "unit, aggregation, extraction, and its string-key path inside each subject result, and "
        "a trial protocol with a finite safety horizon. "
        "Keep hard rules, hidden-data handling, telemetry, runtime limits, and evaluator approach "
        "in the compact derived_setup object. Declare arm symmetry only when swapping algorithm "
        "arms is scientifically meaningful; otherwise explain why it is not applicable. A "
        "human-derived section must reference its decision ID. Use repository citations with "
        "kind=repository and controller citations with kind=controller plus a known rule_id. "
        "Return only JSON.\n\n"
        + json.dumps(
            {
                "task_id": setup["task_id"],
                "revision": revision,
                "confirmed_decisions": decisions["decisions"],
                "late_dependency_requirements": setup.get("late_dependencies", []),
                "required_human_decisions": ["objective", "outcome", "policy_boundary"],
                "design_sections": [
                    "objective", "policy", "environment_adapter", "outcome", "trial",
                    "derived_setup", "conformance", "direct_dependencies",
                ],
                "controller_owned_contract": SETUP_CONTROLLER_CONTRACT,
                "controller_capabilities": SETUP_CAPABILITIES,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    attempts = directory / "setup" / "discovery" / "attempts"
    attempt = 1 + len(tuple(attempts.glob("*"))) if attempts.is_dir() else 1
    if progress is not None:
        progress({"stage": "repository inspection", "status": "started"})
    try:
        value = _agent_run(
            root=attempts / f"{attempt:04d}",
            worktree=subject,
            schema_value=batch_schema(),
            output_name="question-batch.public.json",
            prompt=prompt,
            writable_worktree=False,
            command_builder=command_builder,
            offline=True,
        )
        value = validate_batch(value, subject=subject, revision=revision)
        save_batch(directory, value)
        if value["design"] is None:
            setup["state"] = "QUESTIONS_REQUIRED"
        else:
            finalize_design(
                directory,
                setup,
                value,
                controller_contract=SETUP_CONTROLLER_CONTRACT,
            )
        _save_setup(directory, setup)
    except Exception:
        if progress is not None:
            progress({"stage": "repository inspection", "status": "failed"})
        raise
    if progress is not None:
        progress({"stage": "repository inspection", "status": "completed"})
    return value


def save_answers(
    directory: Path,
    setup: dict[str, Any],
    answers: Mapping[str, Any],
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    discovery = json.loads(
        (directory / "setup" / "discovery.public.json").read_text(encoding="utf-8")
    )
    presentation = setup_presentation(discovery)
    required = {item["id"] for item in presentation["open_questions"]}
    if set(answers) == set(QUESTION_IDS):
        overrides = answers
        answers = {}
        required = set()
    if set(answers) != required or any(
        not isinstance(value, str) or not value.strip() for value in answers.values()
    ):
        raise ValidationError("setup answers must resolve every open clarification once")
    overrides = {} if overrides is None else overrides
    if not set(overrides) <= set(QUESTION_IDS) or any(
        not isinstance(value, str) or not value.strip() for value in overrides.values()
    ):
        raise ValidationError("setup overrides name unknown or empty canonical fields")
    normalized = {
        "schema_version": 2,
        "brief_sha256": discovery.get("brief_sha256"),
        "proposal": presentation["proposal"],
        "answers": {name: answers[name].strip() for name in sorted(answers)},
        "overrides": {name: overrides[name].strip() for name in sorted(overrides)},
    }
    atomic_write_json(
        directory / "setup" / "answers.public.json",
        normalized,
    )
    resolved = resolve_fields(
        presentation, normalized["answers"], normalized["overrides"]
    )
    atomic_write_json(
        directory / "setup" / "legacy-resolved.public.json",
        {
            "schema_version": 1,
            "brief_sha256": normalized["brief_sha256"],
            "fields": resolved,
            "controller_owned_contract": SETUP_CONTROLLER_CONTRACT,
        },
    )
    setup["state"] = "BUILD_REQUIRED"
    _save_setup(directory, setup)
    return normalized


def resolve_fields(
    presentation: Mapping[str, Any],
    answers: Mapping[str, str],
    overrides: Mapping[str, str],
) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in presentation["proposal"]}
    amendments_by_field: dict[str, list[str]] = {}
    for question in presentation["open_questions"]:
        answer = answers.get(question["id"])
        if answer is not None:
            for identifier in question["affected_fields"]:
                amendments_by_field.setdefault(identifier, []).append(answer)
    resolved = []
    for identifier in QUESTION_IDS:
        item = by_id[identifier]
        answer = overrides.get(identifier)
        if answer is None:
            amendments = amendments_by_field.get(identifier, [])
            answer = item["proposed_answer"]
            if amendments and answer is None:
                answer = amendments[0]
            elif amendments and amendments[0] != answer:
                answer = f"{answer}\nHuman clarification: {amendments[0]}"
        resolved.append({**item, "resolved_answer": answer})
    return resolved


def _safe_files(
    root: Path,
    records: Sequence[Mapping[str, Any]],
    *,
    forbidden_roots: frozenset[str] = frozenset(),
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    seen: set[Path] = set()
    for record in records:
        relative = Path(record["path"])
        if (
            relative.is_absolute()
            or relative == Path(".")
            or ".." in relative.parts
            or ".git" in relative.parts
            or (relative.parts and relative.parts[0] in forbidden_roots)
            or relative in seen
        ):
            raise StateError("setup agent proposed an unsafe file path")
        seen.add(relative)
        target = root / relative
        if target.exists() and target.is_symlink():
            raise StateError("setup agent proposed overwriting a symlink")
        atomic_write_text(target, record["content"])


def _fresh_staging_roots(
    directory: Path, setup: Mapping[str, Any], attempt: int
) -> tuple[Path, Path, Path, Path]:
    """Create isolated trees for generated files and all readiness checks."""
    staging = directory / "setup" / "staging" / f"{attempt:04d}"
    if staging.exists():
        staged_subject = staging / "subject"
        if staged_subject.exists():
            subprocess.run(
                [
                    "git",
                    "-C",
                    setup["subject"],
                    "worktree",
                    "remove",
                    "--force",
                    str(staged_subject),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    subject = staging / "subject"
    _git(Path(setup["subject"]), "worktree", "prune")
    completed = subprocess.run(
        [
            "git",
            "-C",
            setup["subject"],
            "worktree",
            "add",
            "--detach",
            str(subject),
            setup["subject_base"],
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise StateError(
            "could not create isolated setup subject: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    environment = staging / "environment"
    evaluator = staging / "evaluator"
    runtime = staging / "runtime"
    environment.mkdir()
    evaluator.mkdir()
    runtime.mkdir()
    return subject, environment, evaluator, runtime


def _materialize_reviewed_files(
    source: Path, destination: Path, records: Sequence[Mapping[str, Any]]
) -> None:
    """Copy only reviewed owned files, refusing to replace user files."""
    for record in records:
        relative = Path(record["path"])
        target = destination / relative
        staged = source / relative
        if target.exists() or target.is_symlink():
            raise StateError(f"setup acceptance would overwrite existing file: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(staged, target)


def _archive_legacy_generated(
    directory: Path, destination: Path, collection: str
) -> None:
    """Move only paths proved to originate in a legacy setup build artifact."""
    paths: set[Path] = set()
    for output in (directory / "setup" / "build" / "attempts").glob(
        "*/output/build.public.json"
    ):
        try:
            value = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for record in value.get(collection, []):
            relative = Path(record.get("path", ""))
            if relative.parts and not relative.is_absolute() and ".." not in relative.parts:
                paths.add(relative)
    if collection == "evaluator_files":
        paths.update(
            {
                Path("_arctl/hook.py"),
                Path("_arctl/api.py"),
                Path("_arctl/evaluator.py"),
                Path("evaluator.manifest.json"),
                Path("test_generated_evaluator.py"),
            }
        )
    archive = directory / "setup" / "legacy-generated" / collection
    for relative in sorted(paths):
        source = destination / relative
        if source.is_file() and not source.is_symlink():
            target = archive / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise StateError(f"legacy setup archive already contains: {target}")
            source.replace(target)


def _python_command(descriptor: Mapping[str, Any], setup: Mapping[str, Any]) -> list[str]:
    python = str(Path(setup["workspace"]) / ".venv" / "bin" / "python")
    target = descriptor["target"]
    if descriptor["kind"] == "script":
        path = Path(target)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
            raise ValidationError("Python script descriptors must name a relative .py file")
        prefix = [python, target]
    else:
        if any(not part.isidentifier() for part in target.split(".")):
            raise ValidationError("Python module descriptors must name a dotted module")
        prefix = [python, "-m", target]
    return [*prefix, *descriptor["arguments"]]


def _command_descriptor(command: Sequence[str]) -> dict[str, Any]:
    """Translate legacy Python argv without changing what it invokes."""
    arguments = list(command)
    if not arguments:
        raise ValidationError("legacy Python command is empty")
    executable = Path(arguments.pop(0)).name
    if executable not in {"python", "python3"} and not executable.startswith("python3."):
        raise ValidationError("legacy public commands must use Python")
    if len(arguments) >= 2 and arguments[0] == "-m":
        return {"kind": "module", "target": arguments[1], "arguments": arguments[2:]}
    if arguments and Path(arguments[0]).suffix == ".py":
        return {"kind": "script", "target": arguments[0], "arguments": arguments[1:]}
    raise ValidationError("legacy public command cannot be represented safely")


def _upgrade_build_v3(value: Mapping[str, Any]) -> dict[str, Any]:
    """Upgrade only controller-owned transport fields; preserve scientific content."""
    if value.get("schema_version") != 3:
        return dict(value)
    upgraded = json.loads(json.dumps(value))
    task = upgraded["task"]
    task["public_checks"] = [
        _command_descriptor(command) for command in task["public_checks"]
    ]
    probe = task["public_probe"]
    probe["execution"] = _command_descriptor(probe.pop("command"))
    for environment_probe in task["environment"]["probes"]:
        environment_probe["execution"] = _command_descriptor(
            environment_probe.pop("command")
        )
    upgraded["schema_version"] = 4
    return upgraded


def _build_semantic_findings(value: Mapping[str, Any]) -> list[str]:
    """Report cross-field rules together so one repair can address all of them."""
    findings: list[str] = []
    evaluator = value.get("evaluator")
    task = value.get("task")
    if not isinstance(evaluator, Mapping) or not isinstance(task, Mapping):
        return findings
    editable = task.get("editable_paths")
    if isinstance(editable, list) and any(
        isinstance(pattern, str) and fnmatchcase("_arctl/hook.py", pattern)
        for pattern in editable
    ):
        findings.append("task editable paths must not include controller setup files")
    environment = task.get("environment")
    if isinstance(environment, Mapping):
        codebases = environment.get("codebases")
        source_ids = {
            item.get("id")
            for item in codebases or []
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        }
        for probe in environment.get("probes") or []:
            if isinstance(probe, Mapping):
                backed_by = probe.get("backed_by")
                unknown = (
                    sorted(set(backed_by) - source_ids)
                    if isinstance(backed_by, list)
                    and all(isinstance(item, str) for item in backed_by)
                    else []
                )
                if unknown:
                    findings.append(
                        f"environment probe {probe.get('id')} names unknown codebases {unknown}"
                    )
    for collection in ("subject_files", "environment_files", "evaluator_files"):
        for record in value.get(collection) or []:
            if not isinstance(record, Mapping):
                continue
            path = Path(str(record.get("path", "")))
            if path.parts and path.parts[0] in {"subject", "environment", "evaluator"}:
                findings.append(
                    f"{collection}: {path} repeats its repository root; paths are root-relative"
                )
    public = evaluator.get("public")
    telemetry = public.get("telemetry") if isinstance(public, Mapping) else None
    if isinstance(telemetry, list):
        names = [item.get("name") for item in telemetry if isinstance(item, Mapping)]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            findings.append(f"telemetry names are duplicated: {duplicates}")
        for item in telemetry:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name", ""))
            if item.get("scope") == "paired" and item.get("value_type") != "number":
                findings.append(f"telemetry {name}: paired metrics must be numeric")
            if item.get("value_type") == "boolean" and item.get("direction") != "contextual":
                findings.append(f"telemetry {name}: boolean metrics must be contextual")
            normalized = name.lower().replace("-", "_")
            if normalized in UNSUPPORTED_PROCESS_TELEMETRY:
                findings.append(
                    f"telemetry {name}: process resource telemetry is unsupported and must be removed"
                )
    calibration = evaluator.get("calibration")
    if isinstance(calibration, Mapping) and calibration.get("supported") is True:
        ladder = calibration.get("ladder")
        if isinstance(ladder, list) and ladder != sorted(set(ladder)):
            findings.append("calibration ladder must be strictly increasing without duplicates")
    trials = task.get("trials")
    if trials == "auto" and isinstance(calibration, Mapping) and calibration.get("supported") is not True:
        findings.append("automatic trials require supported calibration")
    return findings


def _validate_build_contract(
    value: Mapping[str, Any], setup: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], TaskConfig]:
    schema_errors = sorted(
        Draft202012Validator(build_schema()).iter_errors(value),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    findings = _build_semantic_findings(value)
    if schema_errors:
        for error in schema_errors:
            location = ".".join(str(item) for item in error.absolute_path) or "root"
            message = (
                "does not match a permitted contract variant"
                if error.validator in {"anyOf", "oneOf"}
                else error.message
            )
            findings.append(f"build response contract at {location}: {message}")
    if schema_errors:
        raise ValidationError("; ".join(findings))
    task = dict(value["task"])
    task["public_checks"] = [
        _python_command(item, setup) for item in task["public_checks"]
    ]
    public_probe = task["public_probe"]
    task["public_probe"] = {
        "command": _python_command(public_probe["execution"], setup),
        "trial_equivalents": public_probe["trial_equivalents"],
    }
    codebases = []
    for source in task["environment"]["codebases"]:
        owner = source["owner"]
        codebases.append(
            {
                "id": source["id"],
                "description": source["description"],
                "repo": setup[owner],
                "commit": (
                    "SETUP_SUBJECT_COMMIT"
                    if owner == "subject"
                    else "SETUP_ENVIRONMENT_COMMIT"
                ),
                "include": source["include"],
            }
        )
    probes = []
    for probe in task["environment"]["probes"]:
        probes.append(
            {
                **{key: value for key, value in probe.items() if key != "execution"},
                "command": _python_command(probe["execution"], setup),
            }
        )
    task["environment"] = {
        "codebases": codebases,
        "probes": probes,
    }
    denied = list(task["denied_paths"])
    if "_arctl/**" not in denied:
        denied.append("_arctl/**")
    task["denied_paths"] = denied
    task.update(
        {
            "schema_version": 5,
            "task_id": setup["task_id"],
            "repo": setup["subject"],
            "evaluator": {
                "repo": setup["evaluator"],
                "commit": "SETUP_EVALUATOR_COMMIT",
            },
            "method": {
                "profile": "serial-v1",
                "allow_unverified_isolation": False,
            },
        }
    )
    errors = [
        finding
        for finding in findings
        if "process resource telemetry is unsupported" in finding
    ]
    if any(fnmatchcase("_arctl/hook.py", pattern) for pattern in task["editable_paths"]):
        errors.append("task contract: editable paths must not include controller setup files")
    try:
        task_config = TaskConfig.from_mapping(task)
    except ValidationError as error:
        errors.append(f"task contract: {error}")
        task_config = None
    manifest = dict(value["evaluator"])
    python = str(Path(setup["workspace"]) / ".venv" / "bin" / "python")
    calibration = manifest["calibration"]
    manifest.update(
        {
            "schema_version": 4,
            "subject_command": [python, "_arctl/subject.py", "{input}", "{output}"],
            "prepare_command": [
                python,
                "_arctl/evaluator.py",
                "prepare",
                "{request}",
                "{response}",
            ],
            "calibrate_command": (
                [
                    python,
                    "_arctl/evaluator.py",
                    "calibrate",
                    "{request}",
                    "{response}",
                ]
                if calibration["supported"]
                else None
            ),
            "score_command": [
                python,
                "_arctl/evaluator.py",
                "score",
                "{request}",
                "{response}",
            ],
        }
    )
    try:
        raw_schemas = manifest["schemas"]
        manifest["schemas"] = {
            "public_case": json.loads(raw_schemas["public_case_json"]),
            "subject_result": json.loads(raw_schemas["subject_result_json"]),
        }
        raw_public = dict(manifest["public"])
        names = [item["name"] for item in raw_public["telemetry"]]
        if len(names) != len(set(names)):
            raise ValidationError("public.telemetry names must not contain duplicates")
        raw_public["telemetry"] = {
            item["name"]: {name: child for name, child in item.items() if name != "name"}
            for item in raw_public["telemetry"]
        }
        manifest["public"] = raw_public
        parsed_manifest = EvaluatorManifest.from_mapping(manifest)
        if parsed_manifest.schema_version != 4:
            raise ValidationError("manifest.schema_version must equal 4 for setup")
        if parsed_manifest.subject_visible_seed:
            raise ValidationError("guided setup requires evaluator-hidden trial seeds")
        if task_config is not None:
            parsed_manifest.validate_trial_setting(task_config.trials)
    except (json.JSONDecodeError, ValidationError) as error:
        errors.append(f"evaluator manifest contract: {error}")
    serialized = json.dumps(task, sort_keys=True)
    missing = sorted(
        placeholder for placeholder in _PLACEHOLDERS if placeholder not in serialized
    )
    if missing:
        errors.append(f"task contract: missing commit placeholders {missing}")
    if any(
        record["path"] == "evaluator.manifest.json"
        for record in value["evaluator_files"]
    ):
        errors.append(
            "evaluator files: evaluator.manifest.json is controller-owned; "
            "use evaluator_manifest"
        )
    for name in ("subject_files", "environment_files", "evaluator_files"):
        for record in value[name]:
            path = Path(record["path"])
            if path.parts and path.parts[0] in {"subject", "environment", "evaluator"}:
                errors.append(
                    f"{name}: {record['path']} repeats its repository root; paths are root-relative"
                )
    source_ids = {source["id"] for source in task["environment"]["codebases"]}
    for probe in task["environment"]["probes"]:
        unknown = sorted(set(probe["backed_by"]) - source_ids)
        if unknown:
            errors.append(
                f"task contract: environment probe {probe['id']} names unknown codebases {unknown}"
            )
    if errors:
        raise ValidationError("; ".join(errors))
    assert task_config is not None
    return task, manifest, task_config


def _validate_authorized_design_match(
    directory: Path,
    task: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> None:
    design = _load_authorized_design(directory)
    expected_paths = [item["pattern"] for item in design["policy"]["editable_paths"]]
    errors: list[str] = []
    if task.get("objective") != design["objective"]["value"]:
        errors.append("task objective differs from the authorized objective")
    if task.get("editable_paths") != expected_paths:
        errors.append("task editable paths differ from the authorized policy boundary")
    public = manifest.get("public")
    trial = manifest.get("trial")
    if not isinstance(public, Mapping) or public.get("statistic") != design["outcome"]["statistic"]:
        errors.append("evaluator statistic differs from the authorized outcome")
    result_schema: Any = manifest.get("schemas", {}).get("subject_result")
    for part in design["outcome"]["result_path"]:
        properties = result_schema.get("properties") if isinstance(result_schema, Mapping) else None
        if not isinstance(properties, Mapping) or part not in properties:
            errors.append("authorized outcome result path is absent from the subject-result schema")
            break
        result_schema = properties[part]
    else:
        result_type = result_schema.get("type") if isinstance(result_schema, Mapping) else None
        if result_type not in {"number", "integer"}:
            errors.append("authorized outcome result path is not numeric in the subject-result schema")
    if not isinstance(trial, Mapping) or trial.get("meaning") != design["trial"]["unit"]:
        errors.append("evaluator trial unit differs from the authorized trial protocol")
    if not isinstance(trial, Mapping) or trial.get("seed_to_case") != design["trial"]["seed_handling"]:
        errors.append("evaluator seed mapping differs from the authorized trial protocol")
    horizon = design["trial"]["horizon"]
    case_schema = manifest.get("schemas", {}).get("public_case")
    case_properties = case_schema.get("properties") if isinstance(case_schema, Mapping) else None
    case_required = case_schema.get("required") if isinstance(case_schema, Mapping) else None
    horizon_schema = (
        case_properties.get(horizon["case_field"])
        if isinstance(case_properties, Mapping)
        else None
    )
    if (
        not isinstance(horizon_schema, Mapping)
        or horizon_schema.get("const") != horizon["limit"]
        or not isinstance(case_required, list)
        or horizon["case_field"] not in case_required
    ):
        errors.append("public cases do not carry the exact authorized finite horizon")
    adapter = design["environment_adapter"]
    codebases = task.get("environment", {}).get("codebases", [])
    owner_repo = adapter["owner"]
    # _validate_build_contract has already resolved owner names to workspace repositories.
    expected_repo = str(Path(task.get("repo", ""))) if owner_repo == "subject" else None
    if owner_repo == "subject" and not any(
        source.get("repo") == expected_repo
        and adapter["source_path"] in source.get("include", [])
        for source in codebases
    ):
        errors.append("task environment does not include the authorized subject adapter source")
    if owner_repo == "environment" and not any(
        adapter["source_path"] in source.get("include", [])
        for source in codebases
        if source.get("repo") != str(Path(task.get("repo", "")))
    ):
        errors.append("task environment does not include the authorized generated adapter source")
    setup_contract = manifest.get("setup_contract")
    expected_setup_contract = {
        "environment_adapter": {
            "entrypoint": adapter["entrypoint"],
            "interface": adapter["interface"],
        },
        "outcome": {
            key: design["outcome"][key]
            for key in ("direction", "unit", "aggregation", "extraction")
        },
        "trial": {
            "termination": design["trial"]["termination"],
            "horizon_unit": design["trial"]["horizon"]["unit"],
        },
        "hard_rules": design["derived_setup"]["hard_rules"],
        "runtime_limits": design["derived_setup"]["runtime_limits"],
    }
    if setup_contract != expected_setup_contract:
        errors.append("evaluator setup contract differs from the authorized setup contract")
    if errors:
        raise ValidationError("; ".join(errors))


def _public_files(root: Path, *, exclude_private: bool = False) -> tuple[Path, ...]:
    return tuple(
        path
        for path in root.rglob("*")
        if path.is_file()
        and ".git" not in path.relative_to(root).parts
        and (not exclude_private or "private" not in path.relative_to(root).parts)
    )


def _tree_hash(root: Path, *, exclude_private: bool = False) -> str:
    digest = hashlib.sha256()
    for path in _public_files(root, exclude_private=exclude_private):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _authorization_bundle_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    for name in (
        "authorized-design.public.json",
        "authorization.public.json",
        "decisions.public.json",
    ):
        path = directory / "setup" / name
        try:
            content = path.read_bytes()
        except OSError as error:
            raise StateError("setup authorization bundle is missing or invalid") from error
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _acceptance_payload(directory: Path, readiness: Mapping[str, Any]) -> dict[str, Any]:
    staging = readiness.get("staging")
    owned = readiness.get("owned_files")
    review_sha256 = readiness.get("review_sha256")
    if (
        readiness.get("schema_version") != 2
        or not isinstance(staging, Mapping)
        or not isinstance(owned, Mapping)
        or not isinstance(review_sha256, str)
    ):
        raise StateError("setup readiness predates complete acceptance binding; rebuild setup")
    task_draft = directory / "task.draft.yaml"
    runtime = staging.get("runtime")
    lock_hash = readiness.get("dependency_lock_sha256")
    if not isinstance(runtime, str) or not isinstance(lock_hash, str):
        raise StateError("setup readiness lacks a dependency lock; rebuild setup")
    lock_path = Path(runtime) / "uv.lock"
    if not lock_path.is_file():
        raise StateError("setup dependency lock is missing; rebuild setup")
    return {
        "schema_version": 2,
        "subject_base": readiness.get("subject_base"),
        "authorization_bundle_sha256": _authorization_bundle_hash(directory),
        "task_draft_sha256": hashlib.sha256(task_draft.read_bytes()).hexdigest(),
        "owned_files": owned,
        "tree_hashes": {
            "subject": _tree_hash(Path(staging["subject"])),
            "environment": _tree_hash(Path(staging["environment"])),
            "evaluator": _tree_hash(Path(staging["evaluator"]), exclude_private=True),
        },
        "review_sha256": review_sha256,
        "dependency_lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
    }


def _acceptance_token(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _owned_paths(owned: Mapping[str, Any], collection: str) -> tuple[str, ...]:
    records = owned.get(collection)
    if not isinstance(records, list):
        raise StateError("setup ownership list is invalid")
    paths: list[str] = []
    for record in records:
        if not isinstance(record, Mapping) or not isinstance(record.get("path"), str):
            raise StateError("setup ownership list is invalid")
        relative = Path(record["path"])
        if (
            relative.is_absolute()
            or relative == Path(".")
            or ".." in relative.parts
            or ".git" in relative.parts
        ):
            raise StateError("setup ownership list contains an unsafe path")
        paths.append(relative.as_posix())
    if len(paths) != len(set(paths)):
        raise StateError("setup ownership list contains duplicate paths")
    return tuple(paths)


def _owned_path_map(owned: Mapping[str, Any]) -> dict[str, list[str]]:
    return {
        collection: list(_owned_paths(owned, collection))
        for collection in ("subject_files", "environment_files", "evaluator_files")
    }


def _recorded_owned_path_map(owned: Mapping[str, Any]) -> dict[str, list[str]]:
    return {
        collection: [
            str(record.get("path"))
            for record in owned.get(collection, [])
            if isinstance(record, Mapping) and isinstance(record.get("path"), str)
        ]
        for collection in ("subject_files", "environment_files", "evaluator_files")
    }


def _run_setup_command(
    command: Sequence[str],
    *,
    cwd: Path,
    root: Path,
    read_paths: Sequence[Path],
    write_paths: Sequence[Path],
    timeout_seconds: int,
    max_output_bytes: int,
    label: str,
    sandboxed: bool = True,
) -> None:
    """Run generated setup code with the normal process and sandbox guarantees."""
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "execution.started"
    managed = (
        sandbox_command(
            marked_command(command, marker),
            cwd=cwd,
            read_paths=(*read_paths, *command_runtime_read_paths(command)),
            write_paths=write_paths,
            profile="arctl-setup",
        )
        if sandboxed
        else tuple(command)
    )
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    (root / "codex-home").mkdir(parents=True, exist_ok=True)
    environment = sanitized_environment(
        codex_home=root / "codex-home",
        writable_home=home,
    )
    environment["PYTHONPYCACHEPREFIX"] = str((home / "pycache").resolve())
    try:
        result = run_or_load_once(
            root / "process",
            managed,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            cwd=cwd,
            env=environment,
        )
    except (ProcessError, StateError) as error:
        raise StateError(f"setup {label} failed: {error}") from error
    if result["return_code"]:
        if sandboxed and not marker.is_file():
            raise StateError(
                f"setup {label} sandbox did not start its command: "
                f"{primary_process_error(root / 'process')}"
            )
        raise StateError(
            f"setup {label} failed: {primary_process_error(root / 'process')}"
        )


def _setup_conformance(directory: Path) -> Mapping[str, Any]:
    value = _load_authorized_design(directory)
    conformance = value.get("conformance")
    if not isinstance(conformance, Mapping):
        raise StateError("authorized setup design has invalid conformance declarations")
    return conformance


def _mutated_subject_output(
    source: Path,
    destination: Path,
    *,
    trial_count: int,
    manifest: EvaluatorManifest,
    result_path: Sequence[str],
) -> None:
    """Create an alternate output by changing only the authorized outcome field."""
    original = json.loads(source.read_text(encoding="utf-8"))
    results = original.get("results")
    if not isinstance(results, list) or not results:
        raise StateError("arm-symmetry check has no subject results")
    numbers: list[float] = []
    for result in results:
        target: Any = result
        for part in result_path:
            if not isinstance(target, Mapping) or part not in target:
                raise StateError("authorized outcome result path is absent from subject output")
            target = target[part]
        if isinstance(target, bool) or not isinstance(target, (int, float)):
            raise StateError("authorized outcome result path is not numeric")
        numbers.append(float(target))
    for direction in (1.0, -1.0):
        changed = json.loads(json.dumps(original))
        for result, number in zip(changed["results"], numbers, strict=True):
            target = result
            for part in result_path[:-1]:
                target = target[part]
            target[result_path[-1]] = number + direction * max(1.0, abs(number) * 0.1)
        atomic_write_json(destination, changed)
        try:
            _validate_subject_output(
                destination,
                subject="candidate",
                trial_count=trial_count,
                manifest=manifest,
            )
        except Exception:
            continue
        return
    raise StateError(
        "antisymmetric comparison requires a schema-valid numeric authorized outcome"
    )


def _protocol_preflight(
    directory: Path,
    task: TaskConfig,
    manifest: EvaluatorManifest,
    *,
    subject: Path,
    evaluator: Path,
    sandboxed: bool = True,
) -> dict[str, Any]:
    """Exercise generated hooks through the exact controller-owned wire format."""
    attempts = directory / "setup" / "preflight" / "attempts"
    attempt = 1 + len(tuple(attempts.glob("*"))) if attempts.is_dir() else 1
    root = attempts / f"{attempt:04d}"
    requests = root / "requests"
    outputs = root / "outputs"
    prepare_output = outputs / "prepare"
    subject_output = outputs / "subject"
    score_output = outputs / "score"
    calibration_output = outputs / "calibration"
    for path in (requests, prepare_output, subject_output, score_output, calibration_output):
        path.mkdir(parents=True, exist_ok=True)
    count = (
        manifest.calibration.ceiling
        if task.trials == "auto"
        else task.trials
    )
    if not isinstance(count, int) or count <= 0:
        raise StateError("setup protocol preflight has no positive trial count")
    seeds = [
        int.from_bytes(
            hashlib.sha256(f"arctl-setup:{index}".encode()).digest()[:8], "big"
        )
        for index in range(count)
    ]
    seeds[0] = 0
    batch = prepare_output / "batch.public.json"
    scoring = prepare_output / "scoring.private.json"
    prepare_request = requests / "prepare.json"
    prepare_response = prepare_output / "response.json"
    atomic_write_json(
        prepare_request,
        {
            "schema_version": 1,
            "operation": "prepare",
            "kind": "primary",
            "experiment_id": 1,
            "trial_count": count,
            "trial_seeds": seeds,
            "public_batch": str(batch.resolve()),
            "private_scoring": str(scoring.resolve()),
        },
    )
    _run_setup_command(
        render_command(
            manifest.prepare_command,
            {"request": prepare_request, "response": prepare_response},
            allowed_roots=(root,),
        ),
        cwd=evaluator,
        root=root / "prepare",
        read_paths=(evaluator,),
        write_paths=(root,),
        timeout_seconds=manifest.limits.timeout_seconds,
        max_output_bytes=manifest.limits.max_output_bytes,
        label="protocol prepare",
        sandboxed=sandboxed,
    )
    _validate_prepare_response(prepare_response, kind="primary", trial_count=count)
    if not scoring.is_file():
        raise StateError("setup protocol prepare response or private scoring is invalid")
    _validate_batch(batch, trial_count=count, manifest=manifest)

    result = subject_output / "result.json"
    _run_setup_command(
        render_command(
            manifest.subject_command,
            {"input": batch, "output": result},
            allowed_roots=(root,),
        ),
        cwd=subject,
        root=root / "subject",
        read_paths=(subject,),
        write_paths=(root,),
        timeout_seconds=manifest.limits.timeout_seconds,
        max_output_bytes=manifest.limits.max_output_bytes,
        label="protocol subject",
        sandboxed=sandboxed,
    )
    _validate_subject_output(
        result, subject="champion", trial_count=count, manifest=manifest
    )

    capabilities = _setup_conformance(directory)

    def prepare_variant(label: str, variant_seeds: Sequence[int]) -> tuple[Path, Path]:
        variant_root = outputs / label
        variant_root.mkdir(parents=True, exist_ok=True)
        variant_batch = variant_root / "batch.public.json"
        variant_scoring = variant_root / "scoring.private.json"
        request = requests / f"prepare-{label}.json"
        response = variant_root / "response.json"
        atomic_write_json(
            request,
            {
                "schema_version": 1,
                "operation": "prepare",
                "kind": "primary",
                "experiment_id": 1,
                "trial_count": count,
                "trial_seeds": list(variant_seeds),
                "public_batch": str(variant_batch.resolve()),
                "private_scoring": str(variant_scoring.resolve()),
            },
        )
        _run_setup_command(
            render_command(
                manifest.prepare_command,
                {"request": request, "response": response},
                allowed_roots=(root,),
            ),
            cwd=evaluator,
            root=root / f"prepare-{label}",
            read_paths=(evaluator,),
            write_paths=(root,),
            timeout_seconds=manifest.limits.timeout_seconds,
            max_output_bytes=manifest.limits.max_output_bytes,
            label=f"protocol prepare {label}",
            sandboxed=sandboxed,
        )
        _validate_prepare_response(response, kind="primary", trial_count=count)
        _validate_batch(variant_batch, trial_count=count, manifest=manifest)
        if not variant_scoring.is_file():
            raise StateError(f"protocol prepare {label} omitted private scoring")
        return variant_batch, variant_scoring

    repeat_batch, repeat_scoring = prepare_variant("repeat-a", seeds)
    if (
        json.loads(repeat_batch.read_text(encoding="utf-8"))
        != json.loads(batch.read_text(encoding="utf-8"))
        or json.loads(repeat_scoring.read_text(encoding="utf-8"))
        != json.loads(scoring.read_text(encoding="utf-8"))
    ):
        raise StateError("same setup reservation is not repeatable")
    alternate_seeds = [
        int.from_bytes(hashlib.sha256(f"arctl-setup-b:{index}".encode()).digest()[:8], "big")
        for index in range(count)
    ]
    alternate_batch, _ = prepare_variant("alternate-b", alternate_seeds)
    final_batch, final_scoring = prepare_variant("repeat-a-after-b", seeds)
    if (
        json.loads(final_batch.read_text(encoding="utf-8"))
        != json.loads(batch.read_text(encoding="utf-8"))
        or json.loads(final_scoring.read_text(encoding="utf-8"))
        != json.loads(scoring.read_text(encoding="utf-8"))
    ):
        raise StateError("setup prepare leaks state across sequential reservations")
    if capabilities.get("seeded_variation") and (
        json.loads(alternate_batch.read_text(encoding="utf-8"))
        == json.loads(batch.read_text(encoding="utf-8"))
    ):
        raise StateError("seeded-variation capability produced identical public cases")

    alternate_subject_result = subject_output / "alternate-b.json"
    _run_setup_command(
        render_command(
            manifest.subject_command,
            {"input": alternate_batch, "output": alternate_subject_result},
            allowed_roots=(root,),
        ),
        cwd=subject,
        root=root / "subject-alternate-b",
        read_paths=(subject,),
        write_paths=(root,),
        timeout_seconds=manifest.limits.timeout_seconds,
        max_output_bytes=manifest.limits.max_output_bytes,
        label="protocol subject alternate reservation",
        sandboxed=sandboxed,
    )
    _validate_subject_output(
        alternate_subject_result,
        subject="candidate",
        trial_count=count,
        manifest=manifest,
    )
    repeated_subject_result = subject_output / "repeat-a-after-b.json"
    _run_setup_command(
        render_command(
            manifest.subject_command,
            {"input": final_batch, "output": repeated_subject_result},
            allowed_roots=(root,),
        ),
        cwd=subject,
        root=root / "subject-repeat-a-after-b",
        read_paths=(subject,),
        write_paths=(root,),
        timeout_seconds=manifest.limits.timeout_seconds,
        max_output_bytes=manifest.limits.max_output_bytes,
        label="protocol subject repeated reservation",
        sandboxed=sandboxed,
    )
    _validate_subject_output(
        repeated_subject_result,
        subject="candidate",
        trial_count=count,
        manifest=manifest,
    )
    if json.loads(repeated_subject_result.read_text(encoding="utf-8")) != json.loads(
        result.read_text(encoding="utf-8")
    ):
        raise StateError("setup subject leaks state or randomness across sequential trials")

    if manifest.calibration.supported:
        calibration_request = requests / "calibrate.json"
        calibration_response = calibration_output / "response.json"
        calibration_value = {
            "schema_version": 2,
            "operation": "calibrate",
            "champion": "setup",
            "evaluator": "setup",
            "manifest": "setup",
            "policy": manifest.calibration.policy,
            "ladder": list(manifest.calibration.ladder),
            "diagnostic": {
                "name": manifest.calibration.diagnostic_name,
                "units": manifest.calibration.diagnostic_units,
                "maximum": manifest.calibration.diagnostic_maximum,
            },
            "champion_output": str(result.resolve()),
        }
        atomic_write_json(calibration_request, calibration_value)
        _run_setup_command(
            render_command(
                manifest.calibrate_command or (),
                {"request": calibration_request, "response": calibration_response},
                allowed_roots=(root,),
            ),
            cwd=evaluator,
            root=root / "calibrate",
            read_paths=(evaluator,),
            write_paths=(root,),
            timeout_seconds=manifest.limits.timeout_seconds,
            max_output_bytes=manifest.limits.max_output_bytes,
            label="protocol calibrate",
            sandboxed=sandboxed,
        )
        calibration_result = json.loads(calibration_response.read_text(encoding="utf-8"))
        assessments = calibration_result.get("assessments")
        if (
            calibration_result.get("schema_version") != 2
            or calibration_result.get("operation") != "calibrate"
            or [item.get("trial_count") for item in assessments or []]
            != list(manifest.calibration.ladder)
            or any(
                isinstance(item.get("diagnostic_value"), bool)
                or not isinstance(item.get("diagnostic_value"), (int, float))
                or not math.isfinite(item["diagnostic_value"])
                or item["diagnostic_value"] < 0
                for item in assessments or []
            )
        ):
            raise StateError("setup protocol calibration response is invalid")

    score_request = requests / "score.json"
    score_response = score_output / "evidence.json"
    atomic_write_json(
        score_request,
        {
            "schema_version": 1,
            "operation": "score",
            "kind": "primary",
            "experiment_id": 1,
            "trial_count": count,
            "private_scoring": str(scoring.resolve()),
            "champion_output": str(result.resolve()),
            "candidate_output": str(result.resolve()),
        },
    )
    _run_setup_command(
        render_command(
            manifest.score_command,
            {"request": score_request, "response": score_response},
            allowed_roots=(root,),
        ),
        cwd=evaluator,
        root=root / "score",
        read_paths=(evaluator,),
        write_paths=(root,),
        timeout_seconds=manifest.limits.timeout_seconds,
        max_output_bytes=manifest.limits.max_output_bytes,
        label="protocol score",
        sandboxed=sandboxed,
    )
    evidence = Evidence.from_mapping(
        json.loads(score_response.read_text(encoding="utf-8")),
        expected_kind="primary",
        expected_trial_count=count,
        allowed_telemetry=manifest.public_telemetry,
        allowed_suspect_reasons=manifest.suspect_reason_codes,
    )
    if evidence.effect_estimate != 0 or evidence.one_sided_lower_bound != 0:
        raise StateError(
            "setup protocol baseline identity check must score exactly zero effect"
        )
    if evidence.suspect_required:
        raise StateError("setup protocol baseline identity check cannot require suspect testing")
    exchangeability = "not_declared"
    if capabilities.get("arm_symmetry") == "antisymmetric":
        alternate_output = subject_output / "exchangeable-alternate.json"
        _mutated_subject_output(
            result,
            alternate_output,
            trial_count=count,
            manifest=manifest,
            result_path=_load_authorized_design(directory)["outcome"]["result_path"],
        )

        def score_variant(label: str, champion: Path, candidate: Path) -> Evidence:
            request = requests / f"score-{label}.json"
            response_root = score_output / label
            response_root.mkdir(parents=True, exist_ok=True)
            response = response_root / "evidence.json"
            atomic_write_json(
                request,
                {
                    "schema_version": 1,
                    "operation": "score",
                    "kind": "primary",
                    "experiment_id": 1,
                    "trial_count": count,
                    "private_scoring": str(scoring.resolve()),
                    "champion_output": str(champion.resolve()),
                    "candidate_output": str(candidate.resolve()),
                },
            )
            _run_setup_command(
                render_command(
                    manifest.score_command,
                    {"request": request, "response": response},
                    allowed_roots=(root,),
                ),
                cwd=evaluator,
                root=root / f"score-{label}",
                read_paths=(evaluator,),
                write_paths=(root,),
                timeout_seconds=manifest.limits.timeout_seconds,
                max_output_bytes=manifest.limits.max_output_bytes,
                label=f"protocol score {label}",
                sandboxed=sandboxed,
            )
            return Evidence.from_mapping(
                json.loads(response.read_text(encoding="utf-8")),
                expected_kind="primary",
                expected_trial_count=count,
                allowed_telemetry=manifest.public_telemetry,
                allowed_suspect_reasons=manifest.suspect_reason_codes,
            )

        forward = score_variant("exchange-forward", result, alternate_output)
        reverse = score_variant("exchange-reverse", alternate_output, result)
        if forward.effect_estimate == 0:
            raise StateError(
                "exchangeable comparison fixture did not produce a nonzero effect"
            )
        if not math.isclose(
            forward.effect_estimate,
            -reverse.effect_estimate,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise StateError(
                "exchangeable comparison effect is not antisymmetric under arm-label swap"
            )
        exchangeability = "passed"
    summary = {
        "schema_version": 1,
        "trial_count": count,
        "seeds": seeds,
        "effect_estimate": evidence.effect_estimate,
        "one_sided_lower_bound": evidence.one_sided_lower_bound,
        "conformance": {
            "same_reservation_repeatability": "passed",
            "sequential_state_isolation": "passed",
            "seeded_variation": (
                "passed" if capabilities.get("seeded_variation") else "not_declared"
            ),
            "arm_symmetry": exchangeability,
        },
        "attempt": attempt,
    }
    atomic_write_json(root / "preflight.public.json", summary)
    atomic_write_json(directory / "setup" / "preflight.public.json", summary)
    return summary


def _evaluator_checks(
    directory: Path, evaluator: Path, runtime_python: str, *, sandboxed: bool = True
) -> None:
    if not tuple(evaluator.glob("test_*.py")):
        raise StateError("generated evaluator must include public unittest coverage")
    attempts = directory / "setup" / "evaluator-checks" / "attempts"
    attempt = 1 + len(tuple(attempts.glob("*"))) if attempts.is_dir() else 1
    root = attempts / f"{attempt:04d}"
    _run_setup_command(
        [
            runtime_python,
            "-m",
            "unittest",
            "discover",
            "-s",
            str(evaluator),
        ],
        cwd=evaluator,
        root=root,
        read_paths=(evaluator,),
        write_paths=(root,),
        timeout_seconds=300,
        max_output_bytes=1_000_000,
        label="evaluator conformance checks",
        sandboxed=sandboxed,
    )


def _public_setup_checks(
    directory: Path,
    task: TaskConfig,
    manifest: EvaluatorManifest,
    *,
    subject: Path,
    sandboxed: bool = True,
) -> None:
    forbidden_command_roots = (
        Path(task.evaluator.repo).resolve(),
        directory.resolve(),
    )
    for command in (*task.public_checks, task.public_probe):
        for argument in command:
            if any(str(forbidden) in argument for forbidden in forbidden_command_roots):
                raise StateError(
                    "generated public command accesses evaluator or task storage"
                )
    attempts = directory / "setup" / "public-checks" / "attempts"
    attempt = 1 + len(tuple(attempts.glob("*"))) if attempts.is_dir() else 1
    attempt_root = attempts / f"{attempt:04d}"
    commands = (
        *(("public check", item) for item in task.public_checks),
        ("public probe", task.public_probe),
    )
    for index, (label, command) in enumerate(commands, start=1):
        root = attempt_root / f"{index:04d}"
        _run_setup_command(
            command,
            cwd=subject,
            root=root,
            read_paths=(subject,),
            write_paths=(root,),
            timeout_seconds=(
                manifest.limits.timeout_seconds if label == "public probe" else 600
            ),
            max_output_bytes=(
                manifest.limits.max_output_bytes
                if label == "public probe"
                else 1_000_000
            ),
            label=label,
            sandboxed=sandboxed,
        )


def _direct_build_schema() -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    paths = {"type": "array", "items": text}
    return _schema(
        {
            "schema_version": {"type": "integer", "const": 1},
            "summary": text,
            "dependencies": {"type": "array", "items": text},
            "subject_files": paths,
            "environment_files": paths,
            "evaluator_files": paths,
        }
    )


def _read_owned_file_records(root: Path, paths: Sequence[str], *, label: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen: set[Path] = set()
    for raw in paths:
        relative = Path(raw)
        if (
            relative.is_absolute()
            or relative == Path(".")
            or ".." in relative.parts
            or ".git" in relative.parts
            or relative in seen
        ):
            raise ValidationError(f"direct setup build declared an unsafe {label} path")
        seen.add(relative)
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise ValidationError(f"direct setup build omitted declared {label} file: {raw}")
        records.append({"path": relative.as_posix(), "content": path.read_text(encoding="utf-8")})
    return records


def build_setup_direct(
    directory: Path,
    setup: dict[str, Any],
    *,
    offline: bool,
    progress: SetupProgress | None = None,
) -> dict[str, Any]:
    """Let the builder write disposable repositories and return only a compact report."""
    resolved_path = directory / "setup" / "authorized-design.public.json"
    if not resolved_path.is_file():
        raise StateError("setup design must be authorized before generation")
    requirements = _load_authorized_design(directory)
    attempts = directory / "setup" / "direct-build" / "attempts"
    attempt = 1 + len(tuple(attempts.glob("*"))) if attempts.is_dir() else 1
    subject, environment, evaluator, runtime = _fresh_staging_roots(
        directory, setup, 10_000 + attempt
    )
    staging_root = subject.parent
    prompt = (
        "Build the authorized arctl setup directly inside the supplied disposable staging "
        "workspace. Network access is disabled. Do not commit. Write subject files only "
        "under subject/, environment files only under environment/, and evaluator files "
        "only under evaluator/. Write subject/_arctl/hook.py implementing run_batch; write "
        "evaluator/_arctl/hook.py implementing prepare, calibrate, and score; and write "
        "evaluator/test_generated_evaluator.py. Do not write controller-owned api.py, "
        "subject.py, evaluator.py, or evaluator.manifest.json. Write task.design.json and "
        "evaluator.design.json at the staging root using the supplied typed build contract's "
        "task and manifest-design shapes. Return a compact report listing only additional "
        "owned file paths relative to each repository, direct dependencies, and a summary; "
        "do not put source code or configuration contents in the response. Include no fixed "
        "hook paths in the owned lists. Prefer existing project conventions and change no "
        "confirmed decision or controller rule.\n\n"
        + json.dumps(
            {
                "staging": {
                    "root": str(staging_root),
                    "subject": str(subject),
                    "environment": str(environment),
                    "evaluator": str(evaluator),
                },
                "requirements": requirements,
                "prior_review_findings": setup.get("prior_review_findings", []),
                "prior_build_findings": setup.get("prior_build_findings", []),
                "controller_owned_contract": SETUP_CONTROLLER_CONTRACT,
                "controller_capabilities": SETUP_CAPABILITIES,
                "typed_build_contract": SETUP_BUILD_CONTRACT,
                "controller_owned_hook_api_source": SETUP_API_MODULE,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if progress is not None:
        progress({"stage": "generation", "status": "started"})
    try:
        report = _agent_run(
            root=attempts / f"{attempt:04d}",
            worktree=staging_root,
            schema_value=_direct_build_schema(),
            output_name="build-report.public.json",
            prompt=prompt,
            writable_worktree=True,
            read_paths=(Path(setup["subject"]),),
            offline=True,
        )
        subject_hook = (subject / "_arctl" / "hook.py").read_text(encoding="utf-8")
        evaluator_hook = (evaluator / "_arctl" / "hook.py").read_text(encoding="utf-8")
        evaluator_test = (evaluator / "test_generated_evaluator.py").read_text(encoding="utf-8")
        task_design = json.loads((staging_root / "task.design.json").read_text(encoding="utf-8"))
        evaluator_design = json.loads(
            (staging_root / "evaluator.design.json").read_text(encoding="utf-8")
        )
        value = {
            "schema_version": 4,
            "summary": report["summary"],
            "dependencies": report["dependencies"],
            "subject_hook": subject_hook,
            "evaluator_hook": evaluator_hook,
            "evaluator_test": evaluator_test,
            "subject_files": _read_owned_file_records(
                subject, report["subject_files"], label="subject"
            ),
            "environment_files": _read_owned_file_records(
                environment, report["environment_files"], label="environment"
            ),
            "evaluator_files": _read_owned_file_records(
                evaluator, report["evaluator_files"], label="evaluator"
            ),
            "task": task_design,
            "evaluator": evaluator_design,
        }
    except Exception:
        if progress is not None:
            progress({"stage": "generation", "status": "failed"})
        raise
    if progress is not None:
        progress({"stage": "generation", "status": "completed"})
    contract_digest = hashlib.sha256(
        json.dumps(build_schema(), sort_keys=True, separators=(",", ":")).encode()
        + SUBJECT_ENTRYPOINT.encode()
        + EVALUATOR_ENTRYPOINT.encode()
        + SETUP_API_MODULE.encode()
    ).hexdigest()
    requirements_digest = hashlib.sha256(
        resolved_path.read_bytes() + contract_digest.encode()
    ).hexdigest()
    internal = attempts / f"{attempt:04d}" / "output" / "build.internal.json"
    atomic_write_json(internal, value)
    setup["pending_build"] = {
        "output": str(internal),
        "requirements_sha256": requirements_digest,
        "contract_sha256": contract_digest,
    }
    _save_setup(directory, setup)
    subprocess.run(
        [
            "git",
            "-C",
            setup["subject"],
            "worktree",
            "remove",
            "--force",
            str(subject),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    shutil.rmtree(staging_root, ignore_errors=True)
    # The normal controller path rematerializes and validates every staged file,
    # then runs review, dependency provisioning, and conformance in sandboxes.
    return build_setup(directory, setup, offline=offline, progress=progress)


def build_setup(
    directory: Path,
    setup: dict[str, Any],
    *,
    offline: bool,
    command_builder: SetupCommandBuilder | None = None,
    review_command_builder: SetupCommandBuilder | None = None,
    progress: SetupProgress | None = None,
) -> dict[str, Any]:
    source_subject = Path(setup["subject"])
    source_evaluator = Path(setup["evaluator"])
    source_environment = Path(setup["environment"])
    if not _clean(source_subject):
        raise StateError("setup requires a clean subject Git worktree")
    resolved_path = directory / "setup" / "authorized-design.public.json"
    requirements = _load_authorized_design(directory)
    prior_readiness = directory / "setup" / "readiness.public.json"
    prior_findings = []
    if prior_readiness.is_file():
        prior_findings = json.loads(prior_readiness.read_text(encoding="utf-8")).get(
            "findings", []
        )
    prior_build_findings = setup.get("prior_build_findings", [])
    attempts = directory / "setup" / "build" / "attempts"
    attempt = 1 + len(tuple(attempts.glob("*"))) if attempts.is_dir() else 1
    prompt = (
        "Create a minimal faithful Python/uv arctl integration from the confirmed "
        "requirements. Return file contents; do not commit, access hidden data, or "
        "put secrets in output. Prefer existing project conventions. Arctl owns every "
        "command, path root, JSON protocol entrypoint, task identity, repository, commit, "
        "method, and manifest serialization. Implement only the typed hooks below. "
        "subject_hook defines run_batch(public_batch)->dict and runs inside each checked-out "
        "candidate. evaluator_hook defines prepare(context)->{public_batch,private_scoring}, "
        "calibrate(context)->list[{trial_count,diagnostic_value}], and score(context)->dict "
        "with hard_rules_pass, effect_estimate, one_sided_lower_bound, suspect_required, "
        "suspect_reason, and telemetry. The evaluator test imports _arctl.hook and tests "
        "those hooks. Each declared paired telemetry metric must be returned as "
        "{champion: finite number, candidate: finite number}; each comparison metric "
        "must be returned as {value: finite number or boolean}. Return exactly the "
        "declared metric names. Never declare or emit wall-time, RSS, memory, or "
        "dependency-state telemetry because the controller cannot observe it. "
        "without private data. File paths are relative to their assigned repository and "
        "must not begin with subject/, environment/, or evaluator/. Environment codebases "
        "select owner subject or environment; evaluator is never environment evidence. "
        "Encode JSON Schemas in the named *_json strings. Every probe backed_by value names "
        "a declared codebase id. Treat controller_owned_contract below as "
        "fixed: do not turn operational "
        "failures into scored outcomes or replace the controller decision mapping. "
        "When a human clarification uses informal terminology, implement its explicit "
        "statistical quantity, unit, scope, and threshold only when unambiguous; "
        "otherwise leave a reviewable defect rather than silently guessing. Return "
        "only the required JSON.\n\n"
        + json.dumps(
            {
                "task_id": setup["task_id"],
                "subject": str(source_subject),
                "environment": str(source_environment),
                "evaluator": str(source_evaluator),
                "requirements": requirements,
                "prior_review_findings": prior_findings,
                "prior_build_findings": prior_build_findings,
                "controller_owned_contract": SETUP_CONTROLLER_CONTRACT,
                "controller_capabilities": SETUP_CAPABILITIES,
                "typed_build_contract": SETUP_BUILD_CONTRACT,
                "controller_owned_hook_api_source": SETUP_API_MODULE,
                "completion_checklist": [
                    "task has every typed task-specific field",
                    "evaluator has every typed manifest-design field",
                    "all hooks implement their complete typed callable contract",
                    "operational failures produce no statistical score",
                    "public evaluator tests exercise evidence, seeds, and scoring",
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    contract_digest = hashlib.sha256(
        json.dumps(build_schema(), sort_keys=True, separators=(",", ":")).encode()
        + SUBJECT_ENTRYPOINT.encode()
        + EVALUATOR_ENTRYPOINT.encode()
        + SETUP_API_MODULE.encode()
    ).hexdigest()
    requirements_digest = hashlib.sha256(
        resolved_path.read_bytes() + contract_digest.encode()
    ).hexdigest()
    pending = setup.get("pending_build")
    pending_path = (
        Path(pending["output"])
        if isinstance(pending, Mapping)
        and pending.get("requirements_sha256") == requirements_digest
        and isinstance(pending.get("output"), str)
        else None
    )
    if command_builder is None and pending_path is None:
        raise StateError(
            "setup generation requires the isolated direct builder; "
            "no validated internal build is pending"
        )
    if progress is not None:
        progress({"stage": "generation", "status": "started"})
    if pending_path is not None and pending_path.is_file():
        value = json.loads(pending_path.read_text(encoding="utf-8"))
        attempt = int(pending_path.parents[1].name)
    elif command_builder is None and prior_build_findings and attempts.is_dir():
        completed_outputs = sorted(attempts.glob("*/output/build.public.json"))
        if not completed_outputs:
            raise StateError("setup recorded build findings without a recoverable output")
        source = completed_outputs[-1]
        try:
            value = _upgrade_build_v3(json.loads(source.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValidationError) as error:
            raise StateError(f"legacy setup build cannot be recovered safely: {error}") from error
        migration = directory / "setup" / "build" / "migrations" / source.parents[1].name
        pending_path = migration / "build.public.json"
        atomic_write_json(pending_path, value)
        setup["pending_build"] = {
            "output": str(pending_path),
            "requirements_sha256": requirements_digest,
            "contract_sha256": contract_digest,
            "migrated_from": str(source),
        }
        _save_setup(directory, setup)
    else:
        try:
            value = _agent_run(
                root=attempts / f"{attempt:04d}",
                worktree=source_subject,
                schema_value=build_schema(),
                output_name="build.public.json",
                prompt=prompt,
                writable_worktree=False,
                read_paths=(source_environment,),
                command_builder=command_builder,
                offline=offline,
                validate_output=False,
            )
        except Exception:
            if progress is not None:
                progress({"stage": "generation", "status": "failed"})
            raise
        pending_path = attempts / f"{attempt:04d}" / "output" / "build.public.json"
        setup["pending_build"] = {
            "output": str(pending_path),
            "requirements_sha256": requirements_digest,
            "contract_sha256": contract_digest,
        }
        _save_setup(directory, setup)
    if progress is not None:
        progress({"stage": "generation", "status": "completed"})
        progress({"stage": "task validation", "status": "started"})
    try:
        task_value, manifest_value, task = _validate_build_contract(value, setup)
        _validate_authorized_design_match(directory, task_value, manifest_value)
    except ValidationError as first_error:
        if command_builder is None and pending_path is not None and pending_path.name == "build.internal.json":
            setup.pop("pending_build", None)
            setup["prior_build_findings"] = [str(first_error)]
            setup["state"] = "BUILD_REQUIRED"
            _save_setup(directory, setup)
            raise StateError(
                "direct staged setup is invalid and requires one fresh bounded repair: "
                + str(first_error)
            ) from first_error
        if progress is not None:
            progress({"stage": "task validation", "status": "failed"})
            progress(
                {
                    "stage": "contract repair",
                    "status": "started",
                    "detail": "attempt 1/1",
                }
            )
        repair_attempt = 1 + len(tuple(attempts.glob("*")))
        assert pending_path is not None
        repair_prompt = (
            "Repair the typed setup output. Return a complete replacement, not a patch. "
            "Correct every validator finding and recheck the supplied completion checklist. "
            "Read the invalid output and confirmed requirements from the supplied paths. "
            "Do not change confirmed requirements or controller-owned rules. Do not return "
            "commands or repository-prefixed file paths. Implement the complete subject, "
            "prepare, calibrate, and score hook contracts. Each probe backed_by list must "
            "contain declared codebase ids. Telemetry must contain exactly the declared "
            "names: paired numeric metrics use {champion,candidate}; comparison metrics "
            "use {value}. Remove unsupported wall-time, RSS, memory, and dependency-state "
            "telemetry rather than filling it with nulls. "
            "Return only JSON.\n\n"
            + json.dumps(
                {
                    "validator_findings": str(first_error),
                    "invalid_output_path": str(pending_path),
                    "requirements_path": str(resolved_path),
                    "controller_owned_contract": SETUP_CONTROLLER_CONTRACT,
                    "typed_build_contract": SETUP_BUILD_CONTRACT,
                    "controller_owned_hook_api_source": SETUP_API_MODULE,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        try:
            value = _agent_run(
                root=attempts / f"{repair_attempt:04d}",
                worktree=source_subject,
                schema_value=build_schema(),
                output_name="build.public.json",
                prompt=repair_prompt,
                writable_worktree=False,
                read_paths=(
                    pending_path,
                    resolved_path,
                    source_environment,
                ),
                command_builder=command_builder,
                offline=offline,
                validate_output=False,
            )
            pending_path = (
                attempts / f"{repair_attempt:04d}" / "output" / "build.public.json"
            )
            setup["pending_build"] = {
                "output": str(pending_path),
                "requirements_sha256": requirements_digest,
                "contract_sha256": contract_digest,
            }
            _save_setup(directory, setup)
            task_value, manifest_value, task = _validate_build_contract(value, setup)
            _validate_authorized_design_match(directory, task_value, manifest_value)
        except (StateError, ValidationError) as repair_error:
            if progress is not None:
                progress(
                    {
                        "stage": "contract repair",
                        "status": "failed",
                        "detail": "attempt 1/1",
                    }
                )
            setup.pop("pending_build", None)
            setup["prior_build_findings"] = [str(first_error), str(repair_error)]
            _save_setup(directory, setup)
            raise StateError(
                "generated setup contract is invalid after one repair: "
                f"initial validation: {first_error}; repair: {repair_error}"
            ) from repair_error
        if progress is not None:
            progress(
                {
                    "stage": "contract repair",
                    "status": "completed",
                    "detail": "attempt 1/1",
                }
            )
            progress({"stage": "task validation", "status": "started"})
    if progress is not None:
        progress({"stage": "task validation", "status": "completed"})
    _validate_dependency_plan(value["dependencies"], directory=directory)
    generated = {
        "subject_files": [
            *value["subject_files"],
            {"path": "_arctl/hook.py", "content": value["subject_hook"]},
            {"path": "_arctl/api.py", "content": SETUP_API_MODULE},
            {"path": "_arctl/subject.py", "content": SUBJECT_ENTRYPOINT},
        ],
        "environment_files": value["environment_files"],
        "evaluator_files": [
            *value["evaluator_files"],
            {"path": "_arctl/hook.py", "content": value["evaluator_hook"]},
            {"path": "_arctl/api.py", "content": SETUP_API_MODULE},
            {"path": "_arctl/evaluator.py", "content": EVALUATOR_ENTRYPOINT},
            {"path": "test_generated_evaluator.py", "content": value["evaluator_test"]},
        ],
    }
    subject, environment, evaluator, runtime = _fresh_staging_roots(
        directory, setup, attempt
    )
    runtime_python = str(Path(setup["workspace"]) / ".venv" / "bin" / "python")
    manifest_value["subject_command"][0] = runtime_python
    manifest_value["prepare_command"][0] = runtime_python
    manifest_value["score_command"][0] = runtime_python
    if manifest_value.get("calibrate_command") is not None:
        manifest_value["calibrate_command"][0] = runtime_python
    parsed_manifest = EvaluatorManifest.from_mapping(manifest_value)
    _safe_files(
        subject,
        generated["subject_files"],
        forbidden_roots=frozenset({"ARCTL_SETUP.md"}),
    )
    _safe_files(environment, value["environment_files"])
    _safe_files(
        evaluator,
        generated["evaluator_files"],
        forbidden_roots=frozenset({"private"}),
    )
    import yaml

    atomic_write_text(
        directory / "task.draft.yaml", yaml.safe_dump(task_value, sort_keys=False)
    )
    atomic_write_json(evaluator / "evaluator.manifest.json", manifest_value)
    generated["evaluator_files"].append(
        {"path": "evaluator.manifest.json", "content": "controller-owned"}
    )
    pyproject = (
        "[project]\nname = \"arctl-workspace-runtime\"\nversion = \"0.0.0\"\n"
        "requires-python = \">=3.11\"\ndependencies = "
        + json.dumps(value["dependencies"])
        + "\n"
    )
    atomic_write_text(runtime / "pyproject.toml", pyproject)
    review_prompt = (
        "Review this generated arctl setup before any dependency is installed or generated "
        "code is executed. Inspect the subject integration, public environment, evaluator "
        "code, manifest, task draft, authorized design, and direct dependency plan. Cover "
        "every required area exactly once: intent_fidelity, grounding, editable_boundary, dependencies, "
        "trial_independence, scoring_statistics, seed_handling, and runtime_behavior. Each "
        "pass or failure needs a short evidence citation to an inspected file; "
        "not_applicable needs a cogent reason. Report every concrete defect. Do not treat an "
        "empty findings list as sufficient coverage. Return only JSON.\n\n"
        + json.dumps(
            {
                "requirements": requirements,
                "task_draft": str(directory / "task.draft.yaml"),
                "subject": str(subject),
                "environment": str(environment),
                "evaluator": str(evaluator),
                "runtime_project": str(runtime / "pyproject.toml"),
                "controller_owned_contract": SETUP_CONTROLLER_CONTRACT,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    review_attempts = directory / "setup" / "review" / "attempts"
    review_attempt = 1 + len(tuple(review_attempts.glob("*"))) if review_attempts.is_dir() else 1
    if progress is not None:
        progress({"stage": "setup review", "status": "started"})
    try:
        static_review = _agent_run(
            root=review_attempts / f"{review_attempt:04d}",
            worktree=subject,
            schema_value=review_schema(),
            output_name="review.public.json",
            prompt=review_prompt,
            writable_worktree=False,
            read_paths=(
                subject,
                environment,
                directory,
                runtime / "pyproject.toml",
                *_public_files(evaluator, exclude_private=True),
            ),
            command_builder=review_command_builder,
            offline=offline,
            validate_output=review_command_builder is None,
        )
        if static_review.get("schema_version") == 1 and review_command_builder is not None:
            legacy_findings = static_review.get("findings", [])
            failed_area = "intent_fidelity" if legacy_findings else None
            static_review = {
                "schema_version": 2,
                "summary": static_review.get("summary", "Legacy test review."),
                "coverage": {
                    area: {
                        "status": "fail" if area == failed_area else "pass",
                        "summary": "Adapted legacy command-builder review result.",
                        "evidence": [
                            {
                                "path": str(directory / "task.draft.yaml"),
                                "location": "line 1",
                                "finding": "The command-builder fixture inspected the generated setup.",
                            }
                        ],
                    }
                    for area in (
                        "intent_fidelity",
                        "grounding",
                        "editable_boundary",
                        "dependencies",
                        "trial_independence",
                        "scoring_statistics",
                        "seed_handling",
                        "runtime_behavior",
                    )
                },
                "findings": legacy_findings,
            }
        _validate_review_evidence(
            static_review,
            roots=(subject, environment, evaluator, directory, runtime),
        )
    except Exception:
        if progress is not None:
            progress({"stage": "setup review", "status": "failed"})
        setup["state"] = "REVIEW_FAILED"
        _save_setup(directory, setup)
        raise
    if progress is not None:
        progress({"stage": "setup review", "status": "completed"})
    if static_review["findings"]:
        setup["state"] = "REVIEW_FAILED"
        setup["prior_review_findings"] = static_review["findings"]
        _save_setup(directory, setup)
        raise StateError("generated setup failed static review before provisioning")
    uv = shutil.which("uv")
    if uv is None:
        raise StateError("uv is required for Python workspace setup")
    configured_index = os.environ.get("ARCTL_PACKAGE_INDEX", "https://pypi.org/simple")
    if hashlib.sha256(configured_index.encode()).hexdigest() != requirements[
        "dependency_source_policy"
    ]["fingerprint"]:
        raise StateError("configured package index changed after setup authorization")
    sync = [
        uv,
        "sync",
        "--project",
        str(runtime),
        "--no-install-project",
        "--default-index",
        configured_index,
        *(("--offline",) if offline else ()),
    ]
    uv_home = runtime / "home"
    uv_home.mkdir(parents=True, exist_ok=True)
    uv_environment = sanitized_environment(
        codex_home=runtime / "codex-home",
        writable_home=uv_home,
    )
    uv_environment["UV_CACHE_DIR"] = str(runtime / ".uv-cache")
    uv_environment["UV_PROJECT_ENVIRONMENT"] = str(Path(setup["workspace"]) / ".venv")
    if progress is not None:
        progress({"stage": "dependencies", "status": "started"})
    sync_command = (
        sandbox_command(
            marked_command(sync, runtime / "uv-sync.started"),
            cwd=runtime,
            read_paths=(),
            write_paths=(runtime, Path(setup["workspace"]) / ".venv"),
            profile="arctl-setup-dependencies",
            network_enabled=not offline,
        )
        if command_builder is None
        else tuple(sync)
    )
    completed = subprocess.run(
        sync_command,
        check=False,
        capture_output=True,
        text=True,
        env=uv_environment,
    )
    if completed.returncode:
        if progress is not None:
            progress({"stage": "dependencies", "status": "failed"})
        if offline:
            raise StateError("workspace dependencies are unavailable offline; rerun setup without --offline")
        raise StateError("uv failed to provision the workspace runtime: " + completed.stderr.strip())
    dependency_lock = runtime / "uv.lock"
    if not dependency_lock.is_file():
        raise StateError("uv provisioning did not produce a dependency lock")
    dependency_lock_sha256 = hashlib.sha256(dependency_lock.read_bytes()).hexdigest()
    if progress is not None:
        progress({"stage": "dependencies", "status": "completed"})
        progress({"stage": "evaluator checks", "status": "started"})
    try:
        _evaluator_checks(
            directory,
            evaluator,
            runtime_python,
            sandboxed=command_builder is None,
        )
    except StateError as error:
        _invalidate_pending_build(
            directory, setup, str(error)
        )
        raise
    if progress is not None:
        progress({"stage": "evaluator checks", "status": "completed"})
        progress({"stage": "protocol preflight", "status": "started"})
    try:
        preflight = _protocol_preflight(
            directory,
            task,
            parsed_manifest,
            subject=subject,
            evaluator=evaluator,
            sandboxed=command_builder is None,
        )
    except Exception as error:
        if progress is not None:
            progress({"stage": "protocol preflight", "status": "failed"})
        _invalidate_pending_build(directory, setup, f"protocol preflight failed: {error}")
        if isinstance(error, StateError):
            raise
        raise StateError(f"generated protocol preflight failed: {error}") from error
    if progress is not None:
        progress({"stage": "protocol preflight", "status": "completed"})
    if progress is not None:
        progress({"stage": "public checks", "status": "started"})
    try:
        _public_setup_checks(
            directory,
            task,
            parsed_manifest,
            subject=subject,
            sandboxed=command_builder is None,
        )
    except StateError as error:
        _invalidate_pending_build(directory, setup, str(error))
        raise
    if progress is not None:
        progress({"stage": "public checks", "status": "completed"})
    review = static_review
    readiness = {
        "schema_version": 2,
        "requirements": "ready",
        "subject": "ready" if value["subject_files"] or _clean(subject) else "blocked",
        "environment": "ready",
        "evaluator": "ready",
        "runtime": "ready",
        "dependencies": value["dependencies"],
        "dependency_lock": str(dependency_lock),
        "dependency_lock_sha256": dependency_lock_sha256,
        "preflight": preflight,
        "review": "ready" if not review["findings"] else "blocked",
        "findings": review["findings"],
        "tree_hashes": {
            "subject": _tree_hash(subject),
            "environment": _tree_hash(environment),
            "evaluator": _tree_hash(evaluator, exclude_private=True),
        },
        "staging": {
            "subject": str(subject),
            "environment": str(environment),
            "evaluator": str(evaluator),
            "runtime": str(runtime),
        },
        "owned_files": generated,
        "owned_files_sha256": _json_sha256(generated),
        "reviewed_owned_paths": _owned_path_map(generated),
        "subject_base": setup["subject_base"],
        "task_contract": task_value,
        "task_draft_sha256": hashlib.sha256(
            (directory / "task.draft.yaml").read_bytes()
        ).hexdigest(),
        "review_sha256": hashlib.sha256(
            json.dumps(review, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    token = _acceptance_token(_acceptance_payload(directory, readiness))
    readiness["acceptance_token"] = token
    atomic_write_json(directory / "setup" / "readiness.public.json", readiness)
    setup["state"] = "READY_FOR_SETUP_ACCEPTANCE" if not review["findings"] else "REVIEW_FAILED"
    setup["acceptance_token"] = token
    setup.pop("pending_build", None)
    setup.pop("prior_build_findings", None)
    _save_setup(directory, setup)
    return readiness


def _reviewed_artifacts(
    directory: Path, setup: Mapping[str, Any], readiness: Mapping[str, Any]
) -> tuple[dict[str, Any], TaskConfig, EvaluatorManifest]:
    import yaml

    try:
        task_value = yaml.safe_load(
            (directory / "task.draft.yaml").read_text(encoding="utf-8")
        )
        if not isinstance(task_value, dict):
            raise ValidationError("task draft must contain one mapping")
        task = TaskConfig.from_mapping(task_value)
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise StateError(f"edited setup task is invalid: {error}") from error
    if (
        task.task_id != setup["task_id"]
        or task.repo != Path(setup["subject"])
        or task.evaluator.repo != Path(setup["evaluator"])
        or task.evaluator.commit != "SETUP_EVALUATOR_COMMIT"
    ):
        raise StateError("edited setup changed controller-owned task fields")
    prior_task = readiness.get("task_contract")
    if not isinstance(prior_task, Mapping) or any(
        task_value.get(field) != prior_task.get(field)
        for field in ("schema_version", "task_id", "repo", "evaluator", "method")
    ):
        raise StateError("edited setup changed controller-owned task fields")
    locks = {
        (source.path, source.commit)
        for source in task.environment_sources
        if source.path is not None
    }
    if (
        (Path(setup["subject"]), "SETUP_SUBJECT_COMMIT") not in locks
        or (Path(setup["environment"]), "SETUP_ENVIRONMENT_COMMIT") not in locks
    ):
        raise StateError("edited setup changed controller-owned environment locks")

    staging = readiness.get("staging")
    owned = readiness.get("owned_files")
    if not isinstance(staging, Mapping) or not isinstance(owned, Mapping):
        raise StateError("setup ownership or staging record is invalid")
    roots = {
        "subject_files": Path(staging["subject"]),
        "environment_files": Path(staging["environment"]),
        "evaluator_files": Path(staging["evaluator"]),
    }
    for collection, root in roots.items():
        for relative_text in _owned_paths(owned, collection):
            path = root / relative_text
            if path.is_symlink() or not path.is_file():
                raise StateError(f"setup-owned path is not one regular file: {path}")

    subject_changes = {
        line[3:]
        for line in _git(
            roots["subject_files"], "status", "--porcelain", "--untracked-files=all"
        ).splitlines()
        if len(line) >= 4
    }
    if subject_changes != set(_owned_paths(owned, "subject_files")):
        raise StateError("edited subject tree differs from setup ownership")
    for collection in ("environment_files", "evaluator_files"):
        actual = {
            path.relative_to(roots[collection]).as_posix()
            for path in _public_files(
                roots[collection], exclude_private=collection == "evaluator_files"
            )
        }
        if actual != set(_owned_paths(owned, collection)):
            raise StateError(f"edited {collection} tree differs from setup ownership")

    fixed = {
        roots["subject_files"] / "_arctl" / "api.py": SETUP_API_MODULE,
        roots["subject_files"] / "_arctl" / "subject.py": SUBJECT_ENTRYPOINT,
        roots["evaluator_files"] / "_arctl" / "api.py": SETUP_API_MODULE,
        roots["evaluator_files"] / "_arctl" / "evaluator.py": EVALUATOR_ENTRYPOINT,
    }
    for path, expected in fixed.items():
        if path.read_text(encoding="utf-8") != expected:
            raise StateError(f"controller-owned setup file was edited: {path}")
    try:
        manifest = EvaluatorManifest.from_mapping(
            json.loads(
                (roots["evaluator_files"] / "evaluator.manifest.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        manifest.validate_trial_setting(task.trials)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        raise StateError(f"edited evaluator manifest is invalid: {error}") from error
    runtime_python = str(Path(setup["workspace"]) / ".venv" / "bin" / "python")
    expected_commands = {
        "subject": (runtime_python, "_arctl/subject.py", "{input}", "{output}"),
        "prepare": (
            runtime_python,
            "_arctl/evaluator.py",
            "prepare",
            "{request}",
            "{response}",
        ),
        "score": (
            runtime_python,
            "_arctl/evaluator.py",
            "score",
            "{request}",
            "{response}",
        ),
    }
    if (
        manifest.schema_version != 4
        or manifest.subject_command != expected_commands["subject"]
        or manifest.prepare_command != expected_commands["prepare"]
        or manifest.score_command != expected_commands["score"]
        or (
            manifest.calibration.supported
            and manifest.calibrate_command
            != (
                runtime_python,
                "_arctl/evaluator.py",
                "calibrate",
                "{request}",
                "{response}",
            )
        )
        or (not manifest.calibration.supported and manifest.calibrate_command is not None)
    ):
        raise StateError("edited setup changed controller-owned evaluator commands")
    return task_value, task, manifest


def review_setup_edits(
    directory: Path,
    setup: dict[str, Any],
    *,
    offline: bool,
    review_command_builder: SetupCommandBuilder | None = None,
    progress: SetupProgress | None = None,
) -> dict[str, Any]:
    readiness_path = directory / "setup" / "readiness.public.json"
    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    current_payload = _acceptance_payload(directory, readiness)
    if readiness.get("acceptance_token") == _acceptance_token(current_payload):
        return readiness

    setup["state"] = "SETUP_EDIT_REVIEW_REQUIRED"
    setup.pop("acceptance_token", None)
    _save_setup(directory, setup)
    prior_payload = {
        "task_draft_sha256": readiness.get("task_draft_sha256"),
        "owned_files_sha256": readiness.get("owned_files_sha256"),
        "owned_paths": readiness.get("reviewed_owned_paths"),
        "tree_hashes": readiness.get("tree_hashes"),
    }
    changed_trees = sorted(
        name
        for name, digest in current_payload["tree_hashes"].items()
        if not isinstance(prior_payload["tree_hashes"], Mapping)
        or prior_payload["tree_hashes"].get(name) != digest
    )
    current_owned_sha256 = _json_sha256(current_payload["owned_files"])
    current_owned_paths = _recorded_owned_path_map(current_payload["owned_files"])
    ownership_changed = prior_payload["owned_files_sha256"] != current_owned_sha256
    prior_paths = prior_payload["owned_paths"]
    prior_flat = {
        f"{collection}:{path}"
        for collection, paths in (
            prior_paths.items() if isinstance(prior_paths, Mapping) else ()
        )
        for path in paths
    }
    current_flat = {
        f"{collection}:{path}"
        for collection, paths in current_owned_paths.items()
        for path in paths
    }
    attempts = directory / "setup" / "edits" / "attempts"
    attempt = 1 + len(tuple(attempts.glob("*"))) if attempts.is_dir() else 1
    root = attempts / f"{attempt:04d}"
    stages = ["setup review"]
    change = {
        "schema_version": 1,
        "prior_acceptance_token": readiness.get("acceptance_token"),
        "task_fields": [],
        "owned_files_changed": ownership_changed,
        "staging_trees": changed_trees,
        "before": prior_payload,
        "after": {
            "task_draft_sha256": current_payload["task_draft_sha256"],
            "owned_files_sha256": current_owned_sha256,
            "tree_hashes": current_payload["tree_hashes"],
        },
        "owned_paths_added": sorted(current_flat - prior_flat),
        "owned_paths_removed": sorted(prior_flat - current_flat),
        "stages": [],
    }
    atomic_write_json(root / "change.public.json", change)
    try:
        task_value, task, manifest = _reviewed_artifacts(directory, setup, readiness)
        old_task = readiness.get("task_contract")
        changed_task_fields = sorted(
            key
            for key in set(task_value) | (set(old_task) if isinstance(old_task, Mapping) else set())
            if not isinstance(old_task, Mapping) or task_value.get(key) != old_task.get(key)
        )
        change["task_fields"] = changed_task_fields
        staging = readiness["staging"]
        subject = Path(staging["subject"])
        evaluator = Path(staging["evaluator"])
        environment = Path(staging["environment"])
        runtime_python = str(Path(setup["workspace"]) / ".venv" / "bin" / "python")
        if "evaluator" in changed_trees:
            stages.insert(0, "evaluator checks")
            if progress is not None:
                progress({"stage": "evaluator checks", "status": "started"})
            _evaluator_checks(
                directory,
                evaluator,
                runtime_python,
                sandboxed=review_command_builder is None,
            )
            if progress is not None:
                progress({"stage": "evaluator checks", "status": "completed"})
        if (
            {"subject", "evaluator"} & set(changed_trees)
            or "trials" in changed_task_fields
        ):
            stages.insert(-1, "protocol preflight")
            if progress is not None:
                progress({"stage": "protocol preflight", "status": "started"})
            preflight = _protocol_preflight(
                directory,
                task,
                manifest,
                subject=subject,
                evaluator=evaluator,
                sandboxed=review_command_builder is None,
            )
            readiness["preflight"] = preflight
            if progress is not None:
                progress({"stage": "protocol preflight", "status": "completed"})
        if "subject" in changed_trees or {
            "public_checks",
            "public_probe",
        } & set(changed_task_fields):
            stages.insert(-1, "public checks")
            if progress is not None:
                progress({"stage": "public checks", "status": "started"})
            _public_setup_checks(
                directory,
                task,
                manifest,
                subject=subject,
                sandboxed=review_command_builder is None,
            )
            if progress is not None:
                progress({"stage": "public checks", "status": "completed"})

        change["stages"] = stages
        atomic_write_json(root / "change.public.json", change)
        requirements = json.loads(
            (directory / "setup" / "authorized-design.public.json").read_text(encoding="utf-8")
        )
        prompt = (
            "Review these edits to a previously reviewed arctl setup. Inspect the complete "
            "current public setup and the saved change record. Report every concrete "
            "integrity, leakage, protocol, runtime, or fidelity defect. Cover intent_fidelity, "
            "grounding, editable_boundary, dependencies, trial_independence, scoring_statistics, "
            "seed_handling, and runtime_behavior with pass, fail, or justified not_applicable "
            "and cite inspected files. Return only JSON.\n\n"
            + json.dumps(
                {
                    "change_record": str(root / "change.public.json"),
                    "requirements": requirements,
                    "task_draft": str(directory / "task.draft.yaml"),
                    "subject": str(subject),
                    "environment": str(environment),
                    "evaluator": str(evaluator),
                    "controller_owned_contract": SETUP_CONTROLLER_CONTRACT,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        if progress is not None:
            progress({"stage": "setup review", "status": "started"})
        review = _agent_run(
            root=root / "review",
            worktree=subject,
            schema_value=review_schema(),
            output_name="review.public.json",
            prompt=prompt,
            writable_worktree=False,
            read_paths=(
                subject,
                environment,
                directory,
                *_public_files(evaluator, exclude_private=True),
            ),
            command_builder=review_command_builder,
            offline=offline,
            validate_output=review_command_builder is None,
        )
        if review.get("schema_version") == 1 and review_command_builder is not None:
            legacy_findings = review.get("findings", [])
            failed_area = "intent_fidelity" if legacy_findings else None
            review = {
                "schema_version": 2,
                "summary": review.get("summary", "Legacy test review."),
                "coverage": {
                    area: {
                        "status": "fail" if area == failed_area else "pass",
                        "summary": "Adapted legacy command-builder review result.",
                        "evidence": [{
                            "path": str(directory / "task.draft.yaml"),
                            "location": "line 1",
                            "finding": "The command-builder fixture inspected the setup.",
                        }],
                    }
                    for area in (
                        "intent_fidelity", "grounding", "editable_boundary", "dependencies",
                        "trial_independence", "scoring_statistics", "seed_handling",
                        "runtime_behavior",
                    )
                },
                "findings": legacy_findings,
            }
        _validate_review_evidence(
            review, roots=(subject, environment, evaluator, directory)
        )
        if progress is not None:
            progress({"stage": "setup review", "status": "completed"})
    except Exception:
        setup["state"] = "EDIT_REVIEW_FAILED"
        _save_setup(directory, setup)
        raise

    readiness.update(
        {
            "schema_version": 2,
            "review": "ready" if not review["findings"] else "blocked",
            "findings": review["findings"],
            "tree_hashes": current_payload["tree_hashes"],
            "task_draft_sha256": current_payload["task_draft_sha256"],
            "owned_files_sha256": current_owned_sha256,
            "reviewed_owned_paths": current_owned_paths,
            "task_contract": task_value,
            "review_sha256": hashlib.sha256(
                json.dumps(review, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    )
    if review["findings"]:
        readiness.pop("acceptance_token", None)
        setup["state"] = "EDIT_REVIEW_FAILED"
    else:
        token = _acceptance_token(_acceptance_payload(directory, readiness))
        readiness["acceptance_token"] = token
        setup["state"] = "READY_FOR_SETUP_ACCEPTANCE"
        setup["acceptance_token"] = token
    atomic_write_json(readiness_path, readiness)
    _save_setup(directory, setup)
    return readiness


def accept_setup(directory: Path, setup: dict[str, Any], token: str) -> TaskConfig:
    if setup.get("state") != "READY_FOR_SETUP_ACCEPTANCE":
        raise StateError("setup is not ready for acceptance")
    if token != setup.get("acceptance_token"):
        raise StateError("setup acceptance token does not match")
    subject = Path(setup["subject"])
    environment = Path(setup["environment"])
    evaluator = Path(setup["evaluator"])
    readiness = json.loads(
        (directory / "setup" / "readiness.public.json").read_text(encoding="utf-8")
    )
    _load_authorized_design(directory)
    current_token = _acceptance_token(_acceptance_payload(directory, readiness))
    if token != readiness.get("acceptance_token") or token != current_token:
        setup["state"] = "SETUP_EDIT_REVIEW_REQUIRED"
        setup.pop("acceptance_token", None)
        _save_setup(directory, setup)
        raise StateError("setup changed after review; rerun setup to review the edits")
    staging = readiness.get("staging")
    owned = readiness.get("owned_files")
    if not isinstance(staging, Mapping) or not isinstance(owned, Mapping):
        raise StateError("setup readiness predates isolated staging; rebuild setup")
    staged_subject = Path(staging["subject"])
    staged_environment = Path(staging["environment"])
    staged_evaluator = Path(staging["evaluator"])
    if _git(subject, "rev-parse", "HEAD") != readiness.get("subject_base"):
        raise StateError("subject changed after setup review")
    for repository in (subject, environment, evaluator):
        if _git(repository, "diff", "--cached", "--name-only", "-z"):
            raise StateError(f"setup acceptance requires an empty Git index: {repository}")
    branch = f"arctl/setup-{setup['task_id']}"
    current = _git(subject, "branch", "--show-current")
    if current != branch:
        exists = bool(
            _git(subject, "show-ref", "--verify", f"refs/heads/{branch}", check=False)
        )
        if exists:
            if _git(subject, "rev-parse", branch) != setup["subject_base"]:
                raise StateError(
                    "setup branch exists at a different commit; preserve or rename it before acceptance"
                )
            _git(subject, "switch", "-q", branch)
        else:
            _git(subject, "switch", "-q", "-c", branch)
    _archive_legacy_generated(directory, environment, "environment_files")
    _archive_legacy_generated(directory, evaluator, "evaluator_files")
    _materialize_reviewed_files(staged_subject, subject, owned["subject_files"])
    _materialize_reviewed_files(staged_environment, environment, owned["environment_files"])
    _materialize_reviewed_files(staged_evaluator, evaluator, owned["evaluator_files"])
    if token != _acceptance_token(_acceptance_payload(directory, readiness)):
        raise StateError("setup changed while acceptance was materializing reviewed files")
    text = (directory / "task.draft.yaml").read_text(encoding="utf-8")
    subject_commit = _commit(
        subject,
        f"Prepare {setup['task_id']} for arctl",
        _owned_paths(owned, "subject_files"),
        staged_subject,
    )
    environment_commit = _commit(
        environment,
        f"Define {setup['task_id']} environment",
        _owned_paths(owned, "environment_files"),
        staged_environment,
    )
    evaluator_commit = _commit(
        evaluator,
        f"Define {setup['task_id']} evaluator",
        _owned_paths(owned, "evaluator_files"),
        staged_evaluator,
    )
    replacements = {
        "SETUP_SUBJECT_COMMIT": subject_commit,
        "SETUP_ENVIRONMENT_COMMIT": environment_commit,
        "SETUP_EVALUATOR_COMMIT": evaluator_commit,
    }
    for placeholder, commit in replacements.items():
        text = text.replace(placeholder, commit)
    if any(placeholder in text for placeholder in _PLACEHOLDERS):
        raise StateError("generated task contains unresolved commit placeholders")
    atomic_write_text(directory / "task.yaml", text)
    task = load_task(directory / "task.yaml")
    if task.evaluator.commit != evaluator_commit:
        raise StateError("accepted task does not lock the generated evaluator")
    environment_locks = {
        (source.path.resolve(), source.commit)
        for source in task.environment_sources
        if source.path is not None
    }
    if (environment.resolve(), environment_commit) not in environment_locks:
        raise StateError("accepted task does not lock the generated environment")
    if (subject.resolve(), subject_commit) not in environment_locks:
        raise StateError("accepted task does not lock the subject interface source")
    setup["state"] = "READY_FOR_APPROVAL"
    design_path = directory / "setup" / "authorized-design.public.json"
    if design_path.is_file():
        design = json.loads(design_path.read_text(encoding="utf-8"))
        setup["evaluator_pattern"] = design["derived_setup"]["evaluator_pattern"]
    else:
        requirements = json.loads(
            (directory / "setup" / "answers.public.json").read_text(encoding="utf-8")
        )
        if requirements.get("schema_version") == 2:
            proposed = {
                item["id"]: item["proposed_answer"] for item in requirements["proposal"]
            }
            setup["evaluator_pattern"] = requirements["overrides"].get(
                "evaluator_pattern",
                requirements["answers"].get("evaluator", proposed["evaluator_pattern"]),
            )
        else:
            setup["evaluator_pattern"] = requirements["answers"]["evaluator_pattern"]
    setup["subject_commit"] = subject_commit
    setup["environment_commit"] = environment_commit
    setup["evaluator_commit"] = evaluator_commit
    _save_setup(directory, setup)
    return task
