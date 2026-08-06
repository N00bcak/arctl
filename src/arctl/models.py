"""Strict data contracts at arctl trust boundaries."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from .errors import ValidationError
from .manifest import TelemetryMetric
from .methods import MethodConfig, legacy_method, parse_method

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
    "environment",
    "evaluator",
    "trials",
    "max_experiments",
}
_STRATEGY_FIELDS = {"model", "reasoning_effort"}
_EXECUTION_FIELDS = {"model", "reasoning_effort"}
_CANDIDATE_REVIEW_FIELDS = {"contract", "checks", "repair_attempts"}
_ENVIRONMENT_FIELDS = {"sources"}
_ENVIRONMENT_FILE_FIELDS = {"id", "kind", "description", "path"}
_ENVIRONMENT_PROBE_FIELDS = {
    "id",
    "kind",
    "description",
    "command",
    "backed_by",
}
_ENVIRONMENT_V4_FIELDS = {"codebases", "probes"}
_ENVIRONMENT_CODEBASE_FIELDS = {"id", "description", "repo", "commit", "include"}
_ENVIRONMENT_V4_PROBE_FIELDS = {"id", "description", "command", "backed_by"}
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
    "strategy_behavior_id",
    "claim",
    "mechanism",
    "viability",
    "evidence_review",
    "expected_effect",
    "expected_telemetry",
    "falsifiers",
    "lineage",
}
_RESEARCH_LINEAGE_FIELDS = {
    "kind",
    "prior_entry_id",
}
_EVIDENCE_REVIEW_FIELDS = {"summary", "citations"}
_EVIDENCE_CITATION_FIELDS = {"entry_id", "bearing", "finding"}


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
class EnvironmentSource:
    identifier: str
    kind: Literal["implementation", "interface", "documentation", "probe"]
    description: str
    path: Path | None = None
    include: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    backed_by: tuple[str, ...] = ()
    commit: str | None = None


@dataclass(frozen=True)
class CandidateReviewConfig:
    contract: str
    checks: tuple[tuple[str, ...], ...]
    repair_attempts: Literal[0, 1]


def _candidate_review(value: Any) -> CandidateReviewConfig:
    if not isinstance(value, Mapping):
        raise ValidationError("candidate_review must be an object")
    _require_exact_fields(value, _CANDIDATE_REVIEW_FIELDS, "candidate_review")
    attempts = value["repair_attempts"]
    if isinstance(attempts, bool) or attempts not in (0, 1):
        raise ValidationError("candidate_review.repair_attempts must equal 0 or 1")
    return CandidateReviewConfig(
        contract=_string(value["contract"], "candidate_review.contract"),
        checks=_commands(value["checks"], "candidate_review.checks"),
        repair_attempts=attempts,
    )


def _environment_sources(value: Any, *, repo: Path) -> tuple[EnvironmentSource, ...]:
    if not isinstance(value, Mapping):
        raise ValidationError("environment must be an object")
    _require_exact_fields(value, _ENVIRONMENT_FIELDS, "environment")
    raw_sources = value["sources"]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValidationError("environment.sources must be a non-empty list")
    sources: list[EnvironmentSource] = []
    identifiers: set[str] = set()
    for index, raw in enumerate(raw_sources):
        label = f"environment.sources[{index}]"
        if not isinstance(raw, Mapping):
            raise ValidationError(f"{label} must be an object")
        kind = raw.get("kind")
        if kind == "probe":
            _require_exact_fields(raw, _ENVIRONMENT_PROBE_FIELDS, label)
            source = EnvironmentSource(
                identifier=_string(raw["id"], f"{label}.id"),
                kind="probe",
                description=_string(raw["description"], f"{label}.description"),
                command=_command(raw["command"], f"{label}.command"),
                backed_by=_string_list(raw["backed_by"], f"{label}.backed_by"),
            )
            if not source.backed_by:
                raise ValidationError(f"{label}.backed_by must not be empty")
        elif kind in {"implementation", "interface", "documentation"}:
            allowed = _ENVIRONMENT_FILE_FIELDS | ({"include"} if "include" in raw else set())
            _require_exact_fields(raw, allowed, label)
            declared = Path(_string(raw["path"], f"{label}.path"))
            include = (
                _string_list(raw["include"], f"{label}.include")
                if "include" in raw
                else ()
            )
            if "include" in raw and not include:
                raise ValidationError(f"{label}.include must not be empty")
            if any(
                Path(pattern).is_absolute() or ".." in Path(pattern).parts
                for pattern in include
            ):
                raise ValidationError(f"{label}.include must stay within its source path")
            source = EnvironmentSource(
                identifier=_string(raw["id"], f"{label}.id"),
                kind=kind,
                description=_string(raw["description"], f"{label}.description"),
                path=declared if declared.is_absolute() else repo / declared,
                include=include,
            )
        else:
            raise ValidationError(f"{label}.kind is invalid")
        if source.identifier in identifiers:
            raise ValidationError(f"duplicate environment source id: {source.identifier}")
        identifiers.add(source.identifier)
        sources.append(source)
    file_ids = {source.identifier for source in sources if source.kind != "probe"}
    for source in sources:
        if source.kind == "probe" and not set(source.backed_by) <= file_ids:
            raise ValidationError(
                f"environment probe {source.identifier} names unknown backing sources"
            )
    return tuple(sources)


def _environment_references(value: Any) -> tuple[EnvironmentSource, ...]:
    if not isinstance(value, Mapping):
        raise ValidationError("environment must be an object")
    _require_exact_fields(value, _ENVIRONMENT_V4_FIELDS, "environment")
    codebases = value["codebases"]
    probes = value["probes"]
    if not isinstance(codebases, list) or not codebases:
        raise ValidationError("environment.codebases must be a non-empty list")
    if not isinstance(probes, list):
        raise ValidationError("environment.probes must be a list")
    sources: list[EnvironmentSource] = []
    identifiers: set[str] = set()
    for index, raw in enumerate(codebases):
        label = f"environment.codebases[{index}]"
        if not isinstance(raw, Mapping):
            raise ValidationError(f"{label} must be an object")
        _require_exact_fields(raw, _ENVIRONMENT_CODEBASE_FIELDS, label)
        identifier = _string(raw["id"], f"{label}.id")
        source_repo = Path(_string(raw["repo"], f"{label}.repo"))
        if not source_repo.is_absolute():
            raise ValidationError(f"{label}.repo must be absolute")
        include = _string_list(raw["include"], f"{label}.include")
        if not include or any(
            Path(pattern).is_absolute() or ".." in Path(pattern).parts
            for pattern in include
        ):
            raise ValidationError(f"{label}.include must stay within its codebase")
        if identifier in identifiers:
            raise ValidationError(f"duplicate environment source id: {identifier}")
        identifiers.add(identifier)
        sources.append(
            EnvironmentSource(
                identifier=identifier,
                kind="implementation",
                description=_string(raw["description"], f"{label}.description"),
                path=source_repo,
                include=include,
                commit=_string(raw["commit"], f"{label}.commit"),
            )
        )
    for index, raw in enumerate(probes):
        label = f"environment.probes[{index}]"
        if not isinstance(raw, Mapping):
            raise ValidationError(f"{label} must be an object")
        _require_exact_fields(raw, _ENVIRONMENT_V4_PROBE_FIELDS, label)
        identifier = _string(raw["id"], f"{label}.id")
        backed_by = _string_list(raw["backed_by"], f"{label}.backed_by")
        if not backed_by or not set(backed_by) <= identifiers:
            raise ValidationError(f"{label}.backed_by names an unknown codebase")
        if identifier in identifiers:
            raise ValidationError(f"duplicate environment source id: {identifier}")
        identifiers.add(identifier)
        sources.append(
            EnvironmentSource(
                identifier=identifier,
                kind="probe",
                description=_string(raw["description"], f"{label}.description"),
                command=_command(raw["command"], f"{label}.command"),
                backed_by=backed_by,
            )
        )
    return tuple(sources)


@dataclass(frozen=True)
class TaskConfig:
    task_id: str
    repo: Path
    objective: str
    editable_paths: tuple[str, ...]
    denied_paths: tuple[str, ...]
    public_checks: tuple[tuple[str, ...], ...]
    public_probe: tuple[str, ...]
    environment_sources: tuple[EnvironmentSource, ...]
    evaluator: EvaluatorRef
    trials: Literal["auto"] | int
    max_experiments: int | None
    strategy_model: str = "gpt-5.6-sol"
    strategy_reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = "medium"
    planning_model: str = "gpt-5.6-sol"
    planning_reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = "medium"
    execution_model: str = "gpt-5.6-terra"
    execution_reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = "medium"
    reflection_model: str = "gpt-5.6-sol"
    reflection_reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = "medium"
    candidate_review: CandidateReviewConfig | None = None
    method: MethodConfig | None = None
    schema_version: int = 3

    @property
    def environment_probes(self) -> tuple[tuple[str, ...], ...]:
        return tuple(
            source.command for source in self.environment_sources if source.kind == "probe"
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> TaskConfig:
        if value.get("schema_version") == 4:
            return _task_v4(value)
        actual = set(value)
        required = _TASK_FIELDS | {"strategy", "execution"}
        allowed = required | {"planning", "reflection", "candidate_review"}
        if not required <= actual or not actual <= allowed:
            missing = sorted(required - actual)
            extra = sorted(actual - allowed)
            raise ValidationError(f"task fields differ: missing={missing}, extra={extra}")
        schema_version = value["schema_version"]
        if schema_version != 3:
            raise ValidationError("task.schema_version must equal 3")
        repo = Path(_string(value["repo"], "repo"))
        if not repo.is_absolute():
            raise ValidationError("repo must be absolute")
        trials = value["trials"]
        if trials != "auto" and (
            isinstance(trials, bool) or not isinstance(trials, int) or trials <= 0
        ):
            raise ValidationError("trials must be 'auto' or a positive integer")
        maximum = value["max_experiments"]
        if maximum == "unlimited":
            maximum = None
        elif isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
            raise ValidationError("max_experiments must be a positive integer or 'unlimited'")
        strategy = value["strategy"]
        if not isinstance(strategy, Mapping):
            raise ValidationError("strategy must be an object")
        _require_exact_fields(strategy, _STRATEGY_FIELDS, "strategy")
        strategy_model = _string(strategy.get("model", "gpt-5.6-sol"), "strategy.model")
        strategy_effort = strategy.get("reasoning_effort", "medium")
        if strategy_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValidationError("strategy.reasoning_effort is invalid")
        execution = value["execution"]
        if not isinstance(execution, Mapping):
            raise ValidationError("execution must be an object")
        _require_exact_fields(execution, _EXECUTION_FIELDS, "execution")
        execution_model = _string(
            execution.get("model", "gpt-5.6-terra"), "execution.model"
        )
        execution_effort = execution.get("reasoning_effort", "medium")
        if execution_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValidationError("execution.reasoning_effort is invalid")
        planning = value.get("planning", strategy)
        if not isinstance(planning, Mapping):
            raise ValidationError("planning must be an object")
        _require_exact_fields(planning, _STRATEGY_FIELDS, "planning")
        planning_model = _string(planning["model"], "planning.model")
        planning_effort = planning["reasoning_effort"]
        if planning_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValidationError("planning.reasoning_effort is invalid")
        reflection = value.get("reflection", strategy)
        if not isinstance(reflection, Mapping):
            raise ValidationError("reflection must be an object")
        _require_exact_fields(reflection, _STRATEGY_FIELDS, "reflection")
        reflection_model = _string(reflection["model"], "reflection.model")
        reflection_effort = reflection["reasoning_effort"]
        if reflection_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValidationError("reflection.reasoning_effort is invalid")
        method = legacy_method(
            strategy_model=strategy_model,
            strategy_effort=strategy_effort,
            planning_model=planning_model,
            planning_effort=planning_effort,
            execution_model=execution_model,
            execution_effort=execution_effort,
            reflection_model=reflection_model,
            reflection_effort=reflection_effort,
        )
        return cls(
            task_id=validate_task_id(value["task_id"]),
            repo=repo,
            objective=_string(value["objective"], "objective"),
            editable_paths=_string_list(value["editable_paths"], "editable_paths"),
            denied_paths=_string_list(value["denied_paths"], "denied_paths"),
            public_checks=_commands(value["public_checks"], "public_checks"),
            public_probe=_command(value["public_probe"], "public_probe"),
            environment_sources=_environment_sources(value["environment"], repo=repo),
            evaluator=EvaluatorRef.from_mapping(value["evaluator"]),
            trials=trials,
            max_experiments=maximum,
            strategy_model=strategy_model,
            strategy_reasoning_effort=strategy_effort,
            planning_model=planning_model,
            planning_reasoning_effort=planning_effort,
            execution_model=execution_model,
            execution_reasoning_effort=execution_effort,
            reflection_model=reflection_model,
            reflection_reasoning_effort=reflection_effort,
            candidate_review=(
                _candidate_review(value["candidate_review"])
                if "candidate_review" in value
                else None
            ),
            method=method,
            schema_version=schema_version,
        )


def _task_v4(value: Mapping[str, Any]) -> TaskConfig:
    required = _TASK_FIELDS | {"method"}
    allowed = required | {"candidate_review"}
    actual = set(value)
    if actual != required and not ("candidate_review" in actual and actual == allowed):
        missing = sorted(required - actual)
        extra = sorted(actual - allowed)
        raise ValidationError(f"task fields differ: missing={missing}, extra={extra}")
    repo = Path(_string(value["repo"], "repo"))
    if not repo.is_absolute():
        raise ValidationError("repo must be absolute")
    trials = value["trials"]
    if trials != "auto" and (
        isinstance(trials, bool) or not isinstance(trials, int) or trials <= 0
    ):
        raise ValidationError("trials must be 'auto' or a positive integer")
    maximum = value["max_experiments"]
    if maximum == "unlimited":
        maximum = None
    elif isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
        raise ValidationError("max_experiments must be a positive integer or 'unlimited'")
    method = parse_method(value["method"])
    strategy = method.pool("strategize")[0]
    planning = method.pool("plan")[0]
    execution = method.pool("execute")[0]
    reflection = method.pool("reflect")[0]
    return TaskConfig(
        task_id=validate_task_id(value["task_id"]),
        repo=repo,
        objective=_string(value["objective"], "objective"),
        editable_paths=_string_list(value["editable_paths"], "editable_paths"),
        denied_paths=_string_list(value["denied_paths"], "denied_paths"),
        public_checks=_commands(value["public_checks"], "public_checks"),
        public_probe=_command(value["public_probe"], "public_probe"),
        environment_sources=_environment_references(value["environment"]),
        evaluator=EvaluatorRef.from_mapping(value["evaluator"]),
        trials=trials,
        max_experiments=maximum,
        strategy_model=strategy.model,
        strategy_reasoning_effort=strategy.reasoning_effort,
        planning_model=planning.model,
        planning_reasoning_effort=planning.reasoning_effort,
        execution_model=execution.model,
        execution_reasoning_effort=execution.reasoning_effort,
        reflection_model=reflection.model,
        reflection_reasoning_effort=reflection.reasoning_effort,
        candidate_review=(
            _candidate_review(value["candidate_review"])
            if "candidate_review" in value
            else None
        ),
        method=method,
        schema_version=4,
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
    strategy_behavior_id: str
    claim: str
    mechanism: str
    viability: str
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
        if actual != _RESEARCH_FIELDS:
            missing = sorted(_RESEARCH_FIELDS - actual)
            extra = sorted(actual - _RESEARCH_FIELDS)
            raise ValidationError(
                f"research request fields differ: missing={missing}, extra={extra}"
            )
        if value["schema_version"] != 2:
            raise ValidationError("research request schema_version must equal 2")
        lineage = value["lineage"]
        if not isinstance(lineage, Mapping):
            raise ValidationError("research request lineage must be an object")
        _require_exact_fields(lineage, _RESEARCH_LINEAGE_FIELDS, "research lineage")
        if lineage["kind"] not in ("new", "refinement"):
            raise ValidationError("research lineage kind is invalid")
        prior = lineage["prior_entry_id"]
        if prior is not None and (not isinstance(prior, str) or not prior):
            raise ValidationError("research lineage prior_entry_id is invalid")
        if lineage["kind"] == "refinement" and prior is None:
            raise ValidationError("a refinement must name a prior ledger entry")
        review = value["evidence_review"]
        if not isinstance(review, Mapping):
            raise ValidationError("evidence_review must be an object")
        _require_exact_fields(review, _EVIDENCE_REVIEW_FIELDS, "evidence_review")
        _string(review["summary"], "evidence_review.summary")
        citations = review["citations"]
        if not isinstance(citations, list):
            raise ValidationError("evidence_review.citations must be a list")
        for index, citation in enumerate(citations):
            if not isinstance(citation, Mapping):
                raise ValidationError(f"evidence citation {index} must be an object")
            _require_exact_fields(citation, _EVIDENCE_CITATION_FIELDS, "evidence citation")
            _string(citation["entry_id"], "evidence citation entry_id")
            if citation["bearing"] not in {"supports", "contradicts", "unresolved"}:
                raise ValidationError("evidence citation bearing is invalid")
            _string(citation["finding"], "evidence citation finding")
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
            strategy_behavior_id=_string(
                value["strategy_behavior_id"], "strategy_behavior_id"
            ),
            claim=_string(value["claim"], "claim"),
            mechanism=_string(value["mechanism"], "mechanism"),
            viability=_string(value["viability"], "viability"),
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
