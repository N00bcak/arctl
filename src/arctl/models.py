"""Strict data contracts at arctl trust boundaries."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from .errors import ValidationError
from .manifest import TelemetryMetric

ComparisonKind = Literal["primary", "suspect"]

_TASK_FIELDS = {
    "schema_version",
    "task_id",
    "repo",
    "objective",
    "editable_paths",
    "denied_paths",
    "public_checks",
    "public_probe",
    "evaluator",
    "trials",
    "max_experiments",
}
_OPTIONAL_TASK_FIELDS = {"strategy", "execution"}
_STRATEGY_FIELDS = {"model", "reasoning_effort"}
_EXECUTION_FIELDS = {"model", "reasoning_effort"}
_EVALUATOR_FIELDS = {"repo", "commit"}
_EVIDENCE_FIELDS = {
    "schema_version",
    "kind",
    "trial_count",
    "hard_rules_pass",
    "comparison",
    "suspect_test",
    "telemetry",
}
_COMPARISON_FIELDS = {"effect_estimate", "one_sided_lower_bound"}
_SUSPECT_FIELDS = {"required", "reason"}
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_RESEARCH_FIELDS = {
    "schema_version",
    "claim",
    "mechanism",
    "expected_effect",
    "expected_telemetry",
    "falsifiers",
}
_RESEARCH_DIRECTION_FIELDS = {
    "kind",
    "prior_entry_id",
    "strategy_direction_id",
    "rationale",
}


def _require_exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValidationError(f"{label} fields differ: missing={missing}, extra={extra}")


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label} must be a non-empty string")
    return value


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValidationError(f"{label} must be a list of non-empty strings")
    return tuple(value)


def _command(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an argument-vector command")
    command = _string_list(value, label)
    if not command:
        raise ValidationError(f"{label} must not be empty")
    return command


def _commands(value: Any, label: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be a list of argument-vector commands")
    return tuple(_command(command, f"{label}[{index}]") for index, command in enumerate(value))


@dataclass(frozen=True)
class EvaluatorRef:
    repo: Path
    commit: str

    @classmethod
    def from_mapping(cls, value: Any) -> EvaluatorRef:
        if not isinstance(value, Mapping):
            raise ValidationError("evaluator must be an object")
        _require_exact_fields(value, _EVALUATOR_FIELDS, "evaluator")
        repo = Path(_string(value["repo"], "evaluator.repo"))
        if not repo.is_absolute():
            raise ValidationError("evaluator.repo must be absolute")
        return cls(repo=repo, commit=_string(value["commit"], "evaluator.commit"))


@dataclass(frozen=True)
class TaskConfig:
    task_id: str
    repo: Path
    objective: str
    editable_paths: tuple[str, ...]
    denied_paths: tuple[str, ...]
    public_checks: tuple[tuple[str, ...], ...]
    public_probe: tuple[str, ...]
    evaluator: EvaluatorRef
    trials: Literal["auto"] | int
    max_experiments: int
    strategy_model: str = "gpt-5.6-sol"
    strategy_reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = "high"
    execution_model: str = "gpt-5.6-terra"
    execution_reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = "medium"
    schema_version: int = 2

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> TaskConfig:
        actual = set(value)
        if not _TASK_FIELDS <= actual or not actual <= _TASK_FIELDS | _OPTIONAL_TASK_FIELDS:
            missing = sorted(_TASK_FIELDS - actual)
            extra = sorted(actual - _TASK_FIELDS - _OPTIONAL_TASK_FIELDS)
            raise ValidationError(f"task fields differ: missing={missing}, extra={extra}")
        schema_version = value["schema_version"]
        if schema_version not in {1, 2}:
            raise ValidationError("task.schema_version must equal 1 or 2")
        if schema_version == 2 and not {"strategy", "execution"} <= actual:
            missing = sorted({"strategy", "execution"} - actual)
            raise ValidationError(
                f"task schema v2 requires explicit agent settings: missing={missing}"
            )
        repo = Path(_string(value["repo"], "repo"))
        if not repo.is_absolute():
            raise ValidationError("repo must be absolute")
        trials = value["trials"]
        if trials != "auto" and (
            isinstance(trials, bool) or not isinstance(trials, int) or trials <= 0
        ):
            raise ValidationError("trials must be 'auto' or a positive integer")
        maximum = value["max_experiments"]
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
            raise ValidationError("max_experiments must be a positive integer")
        strategy = value.get("strategy", {})
        if not isinstance(strategy, Mapping):
            raise ValidationError("strategy must be an object")
        if strategy:
            _require_exact_fields(strategy, _STRATEGY_FIELDS, "strategy")
        strategy_model = _string(strategy.get("model", "gpt-5.6-sol"), "strategy.model")
        strategy_effort = strategy.get("reasoning_effort", "high")
        if strategy_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValidationError("strategy.reasoning_effort is invalid")
        execution = value.get("execution", {})
        if not isinstance(execution, Mapping):
            raise ValidationError("execution must be an object")
        if execution:
            _require_exact_fields(execution, _EXECUTION_FIELDS, "execution")
        execution_model = _string(
            execution.get("model", "gpt-5.6-terra"), "execution.model"
        )
        execution_effort = execution.get("reasoning_effort", "medium")
        if execution_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValidationError("execution.reasoning_effort is invalid")
        return cls(
            task_id=validate_task_id(value["task_id"]),
            repo=repo,
            objective=_string(value["objective"], "objective"),
            editable_paths=_string_list(value["editable_paths"], "editable_paths"),
            denied_paths=_string_list(value["denied_paths"], "denied_paths"),
            public_checks=_commands(value["public_checks"], "public_checks"),
            public_probe=_command(value["public_probe"], "public_probe"),
            evaluator=EvaluatorRef.from_mapping(value["evaluator"]),
            trials=trials,
            max_experiments=maximum,
            strategy_model=strategy_model,
            strategy_reasoning_effort=strategy_effort,
            execution_model=execution_model,
            execution_reasoning_effort=execution_effort,
            schema_version=schema_version,
        )


def validate_task_id(value: Any) -> str:
    task_id = _string(value, "task_id")
    if not _TASK_ID.fullmatch(task_id):
        raise ValidationError(
            "task_id must contain only letters, numbers, dots, underscores, and hyphens"
        )
    return task_id


@dataclass(frozen=True)
class ResearchRequest:
    claim: str
    mechanism: str
    expected_effect: str
    expected_telemetry: Mapping[str, str]
    falsifiers: tuple[str, ...]

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        allowed_telemetry: Sequence[str],
    ) -> ResearchRequest:
        actual = set(value)
        if actual not in (_RESEARCH_FIELDS, _RESEARCH_FIELDS | {"direction"}):
            missing = sorted(_RESEARCH_FIELDS - actual)
            extra = sorted(actual - _RESEARCH_FIELDS - {"direction"})
            raise ValidationError(
                f"research request fields differ: missing={missing}, extra={extra}"
            )
        if value["schema_version"] != 1:
            raise ValidationError("research request schema_version must equal 1")
        direction = value.get("direction")
        if direction is not None:
            if not isinstance(direction, Mapping):
                raise ValidationError("research request direction must be an object")
            _require_exact_fields(direction, _RESEARCH_DIRECTION_FIELDS, "research direction")
            if direction["kind"] not in ("new", "refinement"):
                raise ValidationError("research direction kind is invalid")
            prior = direction["prior_entry_id"]
            if prior is not None and (not isinstance(prior, str) or not prior):
                raise ValidationError("research direction prior_entry_id is invalid")
            if direction["kind"] == "refinement" and prior is None:
                raise ValidationError("a refinement must name a prior ledger entry")
            _string(direction["strategy_direction_id"], "research direction strategy_direction_id")
            _string(direction["rationale"], "research direction rationale")
        telemetry = value["expected_telemetry"]
        if not isinstance(telemetry, Mapping) or any(
            not isinstance(name, str)
            or not name
            or not isinstance(expectation, str)
            or not expectation
            for name, expectation in telemetry.items()
        ):
            raise ValidationError(
                "expected_telemetry must map names to non-empty expectations"
            )
        unapproved = set(telemetry) - set(allowed_telemetry)
        if unapproved:
            raise ValidationError(
                f"research request names unapproved telemetry: {sorted(unapproved)}"
            )
        falsifiers = _string_list(value["falsifiers"], "falsifiers")
        if not falsifiers:
            raise ValidationError("falsifiers must not be empty")
        return cls(
            claim=_string(value["claim"], "claim"),
            mechanism=_string(value["mechanism"], "mechanism"),
            expected_effect=_string(value["expected_effect"], "expected_effect"),
            expected_telemetry=dict(telemetry),
            falsifiers=falsifiers,
        )


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{label} must be finite")
    return result


@dataclass(frozen=True)
class Evidence:
    kind: ComparisonKind
    trial_count: int
    hard_rules_pass: bool
    effect_estimate: float
    one_sided_lower_bound: float
    suspect_required: bool
    suspect_reason: str | None
    telemetry: Mapping[str, Any]

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        expected_kind: ComparisonKind,
        expected_trial_count: int,
        allowed_telemetry: Mapping[str, TelemetryMetric] | None = None,
        allowed_suspect_reasons: Sequence[str] = (),
    ) -> Evidence:
        _require_exact_fields(value, _EVIDENCE_FIELDS, "evidence")
        if value["schema_version"] != 1:
            raise ValidationError("evidence.schema_version must equal 1")
        if value["kind"] != expected_kind:
            raise ValidationError("evidence kind does not match the comparison")
        count = value["trial_count"]
        if isinstance(count, bool) or count != expected_trial_count:
            raise ValidationError("evidence trial_count does not match the reservation")
        hard_rules = value["hard_rules_pass"]
        if not isinstance(hard_rules, bool):
            raise ValidationError("hard_rules_pass must be boolean")

        comparison = value["comparison"]
        if not isinstance(comparison, Mapping):
            raise ValidationError("comparison must be an object")
        _require_exact_fields(comparison, _COMPARISON_FIELDS, "comparison")
        estimate = _finite_number(comparison["effect_estimate"], "effect_estimate")
        lower = _finite_number(
            comparison["one_sided_lower_bound"], "one_sided_lower_bound"
        )
        if lower > estimate:
            raise ValidationError("one_sided_lower_bound must not exceed effect_estimate")

        suspect = value["suspect_test"]
        if not isinstance(suspect, Mapping):
            raise ValidationError("suspect_test must be an object")
        _require_exact_fields(suspect, _SUSPECT_FIELDS, "suspect_test")
        required = suspect["required"]
        reason = suspect["reason"]
        if not isinstance(required, bool):
            raise ValidationError("suspect_test.required must be boolean")
        if reason is not None and not isinstance(reason, str):
            raise ValidationError("suspect_test.reason must be a string or null")
        if required and (not reason or reason not in allowed_suspect_reasons):
            raise ValidationError("suspect test requires an approved reason")
        if required and (not hard_rules or estimate <= 0 or lower <= 0):
            raise ValidationError("suspect test may only hold an otherwise accepted result")
        if not required and reason is not None:
            raise ValidationError("suspect reason must be null when no test is required")

        telemetry = validate_telemetry(
            value["telemetry"],
            metrics=allowed_telemetry or {},
            suspect=expected_kind == "suspect",
        )
        if expected_kind == "suspect" and (required or reason is not None or telemetry):
            raise ValidationError("suspect evidence cannot request another test or add telemetry")

        return cls(
            kind=expected_kind,
            trial_count=count,
            hard_rules_pass=hard_rules,
            effect_estimate=estimate,
            one_sided_lower_bound=lower,
            suspect_required=required,
            suspect_reason=reason,
            telemetry=dict(telemetry),
        )


def validate_telemetry(
    value: Any,
    *,
    metrics: Mapping[str, TelemetryMetric],
    suspect: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("telemetry must be an object")
    expected = set() if suspect else set(metrics)
    extra = set(value) - expected
    if extra:
        raise ValidationError(f"unapproved telemetry fields: {sorted(extra)}")
    if set(value) != expected:
        raise ValidationError(
            "telemetry fields differ: "
            f"missing={sorted(expected - set(value))}, "
            f"extra={sorted(set(value) - expected)}"
        )
    validated: dict[str, Any] = {}
    for name, metric in metrics.items():
        raw = value[name]
        if not isinstance(raw, Mapping):
            raise ValidationError(f"telemetry.{name} must be an object")
        fields = {"champion", "candidate"} if metric.scope == "paired" else {"value"}
        _require_exact_fields(raw, fields, f"telemetry.{name}")
        item: dict[str, Any] = {}
        for field in fields:
            telemetry_value = raw[field]
            if metric.value_type == "boolean":
                if not isinstance(telemetry_value, bool):
                    raise ValidationError(f"telemetry.{name}.{field} must be boolean")
                item[field] = telemetry_value
            else:
                item[field] = _finite_number(
                    telemetry_value, f"telemetry.{name}.{field}"
                )
        validated[name] = item
    return validated
