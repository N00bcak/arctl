"""Experiment outcome resolution and safe public feedback."""

from __future__ import annotations

import math
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any

from .decisions import Decision, decide
from .errors import StateError, ValidationError
from .manifest import TelemetryMetric
from .models import Evidence, validate_telemetry


@dataclass(frozen=True)
class ExperimentOutcome:
    decision: Decision
    primary: Evidence
    suspect: Evidence | None

    @property
    def final(self) -> bool:
        return self.decision is not Decision.PROVISIONAL

    @property
    def may_promote(self) -> bool:
        return self.decision is Decision.ACCEPT


def resolve_outcome(
    primary: Evidence,
    suspect: Evidence | None = None,
) -> ExperimentOutcome:
    if primary.kind != "primary":
        raise StateError("the primary comparison evidence has the wrong kind")
    primary_decision = decide(primary)
    if primary_decision is Decision.PROVISIONAL:
        if suspect is None:
            return ExperimentOutcome(Decision.PROVISIONAL, primary, None)
        if suspect.kind != "suspect":
            raise StateError("suspect comparison evidence has the wrong kind")
        return ExperimentOutcome(decide(suspect), primary, suspect)
    if suspect is not None:
        raise StateError("suspect evidence exists without an approved primary trigger")
    return ExperimentOutcome(primary_decision, primary, None)


def build_public_result(
    *,
    experiment_id: int,
    hypothesis: str,
    champion_before: str,
    candidate: str,
    statistic: str,
    outcome: ExperimentOutcome,
    tests_pass: bool,
) -> dict[str, Any]:
    if not outcome.final:
        raise StateError("a provisional experiment cannot be published as final")
    champion_after = candidate if outcome.may_promote else champion_before
    evidence_items = (outcome.primary,) + (
        (outcome.suspect,) if outcome.suspect is not None else ()
    )
    comparisons = [public_comparison(evidence) for evidence in evidence_items]
    return {
        "experiment_id": experiment_id,
        "hypothesis": hypothesis,
        "champion_before": champion_before,
        "candidate": candidate,
        "champion_after": champion_after,
        "decision": outcome.decision.value,
        "evaluation": {
            "statistic": statistic,
            "comparisons": comparisons,
        },
        "constraints": {"tests": "PASS" if tests_pass else "FAIL"},
        "telemetry": publish_telemetry(outcome.primary.telemetry),
    }


def publish_telemetry(telemetry: Mapping[str, Any]) -> dict[str, Any]:
    published: dict[str, Any] = {}
    for name, raw in telemetry.items():
        item = dict(raw)
        if set(item) == {"champion", "candidate"}:
            item["delta"] = item["candidate"] - item["champion"]
        published[name] = item
    return published


def public_comparison(evidence: Evidence) -> dict[str, Any]:
    return {
        "kind": evidence.kind,
        "trials": evidence.trial_count,
        "effect_estimate": evidence.effect_estimate,
        "one_sided_lower_bound": evidence.one_sided_lower_bound,
        "suspect_test_required": evidence.suspect_required,
        "suspect_test_reason": evidence.suspect_reason,
    }


def validate_public_result(
    value: Any,
    *,
    allowed_telemetry: Mapping[str, TelemetryMetric],
    allowed_suspect_reasons: Sequence[str],
    expected_statistic: str,
) -> dict[str, Any]:
    """Validate the only experiment data that a later research session may see."""
    base_fields = {
        "experiment_id",
        "hypothesis",
        "champion_before",
        "candidate",
        "champion_after",
        "decision",
        "evaluation",
        "constraints",
        "telemetry",
    }
    if not isinstance(value, Mapping) or set(value) not in (
        base_fields,
        base_fields | {"failure"},
        base_fields | {"failure", "failure_detail"},
    ):
        raise StateError("public result fields are invalid")
    identifier = value["experiment_id"]
    strings = ("hypothesis", "champion_before", "candidate", "champion_after")
    if (
        isinstance(identifier, bool)
        or not isinstance(identifier, int)
        or identifier <= 0
        or any(not isinstance(value[name], str) or not value[name] for name in strings)
        or value["decision"]
        not in {
            Decision.ACCEPT.value,
            Decision.ARCHIVE.value,
            Decision.REJECT.value,
            Decision.INVALID.value,
        }
    ):
        raise StateError("public result values are invalid")
    if "failure" in value and value["failure"] not in (
        "candidate_execution",
        "system_execution",
    ):
        raise StateError("public result failure is invalid")
    if "failure_detail" in value and (
        "failure" not in value
        or not isinstance(value["failure_detail"], str)
        or not value["failure_detail"]
    ):
        raise StateError("public result failure detail is invalid")

    constraints = value["constraints"]
    if (
        not isinstance(constraints, Mapping)
        or set(constraints) != {"tests"}
        or constraints["tests"] not in ("PASS", "FAIL")
    ):
        raise StateError("public result constraints are invalid")
    evaluation = value["evaluation"]
    if (
        not isinstance(evaluation, Mapping)
        or set(evaluation) != {"statistic", "comparisons"}
        or (
            evaluation["statistic"] is not None
            and (
                not isinstance(evaluation["statistic"], str)
                or not evaluation["statistic"]
            )
        )
        or not isinstance(evaluation["comparisons"], list)
        or len(evaluation["comparisons"]) > 2
        or (
            evaluation["comparisons"]
            and evaluation["statistic"] != expected_statistic
        )
    ):
        raise StateError("public result evaluation is invalid")
    kinds: list[str] = []
    comparison_fields = {
        "kind",
        "trials",
        "effect_estimate",
        "one_sided_lower_bound",
        "suspect_test_required",
        "suspect_test_reason",
    }
    for comparison in evaluation["comparisons"]:
        if not isinstance(comparison, Mapping) or set(comparison) != comparison_fields:
            raise StateError("public comparison fields are invalid")
        kind = comparison["kind"]
        trials = comparison["trials"]
        estimate = comparison["effect_estimate"]
        lower = comparison["one_sided_lower_bound"]
        required = comparison["suspect_test_required"]
        reason = comparison["suspect_test_reason"]
        if (
            kind not in ("primary", "suspect")
            or isinstance(trials, bool)
            or not isinstance(trials, int)
            or trials <= 0
            or isinstance(estimate, bool)
            or not isinstance(estimate, (int, float))
            or not math.isfinite(estimate)
            or isinstance(lower, bool)
            or not isinstance(lower, (int, float))
            or not math.isfinite(lower)
            or lower > estimate
            or not isinstance(required, bool)
            or (reason is not None and not isinstance(reason, str))
            or (not required and reason is not None)
            or (required and reason not in allowed_suspect_reasons)
            or (kind == "suspect" and (required or reason is not None))
        ):
            raise StateError("public comparison values are invalid")
        kinds.append(kind)
    if kinds not in ([], ["primary"], ["primary", "suspect"]):
        raise StateError("public comparison order is invalid")
    if (
        value["decision"] == Decision.ACCEPT.value
        and value["champion_after"] != value["candidate"]
    ) or (
        value["decision"] != Decision.ACCEPT.value
        and value["champion_after"] != value["champion_before"]
    ):
        raise StateError("public result champion transition is invalid")

    telemetry = value["telemetry"]
    expected_telemetry = set(allowed_telemetry) if kinds else set()
    if not isinstance(telemetry, Mapping) or set(telemetry) != expected_telemetry:
        raise StateError("public result telemetry is invalid")
    private_shape: dict[str, Any] = {}
    metrics = allowed_telemetry if kinds else {}
    for name, metric in metrics.items():
        raw = telemetry[name]
        if not isinstance(raw, Mapping):
            raise StateError("public result telemetry is invalid")
        expected = (
            {"champion", "candidate", "delta"}
            if metric.scope == "paired"
            else {"value"}
        )
        if set(raw) != expected:
            raise StateError("public result telemetry is invalid")
        item = dict(raw)
        if metric.scope == "paired":
            delta = item.pop("delta")
            if (
                isinstance(delta, bool)
                or not isinstance(delta, (int, float))
                or not math.isfinite(delta)
                or not math.isclose(
                    float(delta),
                    float(item["candidate"]) - float(item["champion"]),
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
            ):
                raise StateError("public result telemetry delta is invalid")
        private_shape[name] = item
    try:
        validate_telemetry(private_shape, metrics=metrics)
    except ValidationError as error:
        raise StateError("public result telemetry is invalid") from error
    return dict(value)
