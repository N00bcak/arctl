"""Approved evaluator-manifest contract."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from .commands import validate_command_template
from .errors import StateError, ValidationError

_FIELDS = {
    "schema_version",
    "subject_command",
    "prepare_command",
    "calibrate_command",
    "score_command",
    "limits",
    "schemas",
    "public",
    "trial",
    "statistics",
    "variation",
    "suspect_test",
    "calibration",
}
_SETUP_CONTRACT_FIELDS = {
    "environment_adapter",
    "outcome",
    "trial",
    "hard_rules",
    "runtime_limits",
}
_SETUP_ADAPTER_FIELDS = {"entrypoint", "interface"}
_SETUP_OUTCOME_FIELDS = {"direction", "unit", "aggregation", "extraction"}
_SETUP_TRIAL_FIELDS = {"termination", "horizon_unit"}
_LIMIT_FIELDS = {"timeout_seconds", "max_output_bytes"}
_SCHEMA_FIELDS = {"public_case", "subject_result"}
_PUBLIC_FIELDS = {"statistic", "subject_interface", "telemetry"}
_TELEMETRY_FIELDS = {
    "description",
    "unit",
    "scope",
    "role",
    "value_type",
    "direction",
}
_TRIAL_FIELDS = {"meaning", "dependence", "seed_to_case", "subject_visible_seed"}
_STATISTIC_FIELDS = {"score", "uncertainty", "positive_effect"}
_VARIATION_FIELDS = {"known", "mitigations"}
_SUSPECT_FIELDS = {"trigger", "reason_codes"}
_CALIBRATION_V1_FIELDS = {"supported", "policy", "ceiling"}
_CALIBRATION_V2_FIELDS = {"supported", "policy", "ladder", "diagnostic"}
_DIAGNOSTIC_FIELDS = {"name", "units", "maximum"}


def _object(value: Any, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} must be an object")
    if set(value) != fields:
        raise ValidationError(
            f"{label} fields differ: "
            f"missing={sorted(fields - set(value))}, extra={sorted(set(value) - fields)}"
        )
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label} must be a non-empty string")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{label} must be a positive integer")
    return value


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValidationError(f"{label} must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise ValidationError(f"{label} must not contain duplicates")
    return tuple(value)


def _reject_external_schema_refs(value: Any, label: str) -> None:
    if isinstance(value, Mapping):
        reference = value.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#"):
            raise ValidationError(f"{label} contains an external JSON Schema reference")
        for child in value.values():
            _reject_external_schema_refs(child, label)
    elif isinstance(value, list):
        for child in value:
            _reject_external_schema_refs(child, label)


@dataclass(frozen=True)
class ProcessLimits:
    timeout_seconds: int
    max_output_bytes: int


@dataclass(frozen=True)
class CalibrationPolicy:
    supported: bool
    policy: str | None
    ceiling: int | None
    ladder: tuple[int, ...]
    diagnostic_name: str | None
    diagnostic_units: str | None
    diagnostic_maximum: float | None
    controller_pilot: bool


@dataclass(frozen=True)
class TelemetryMetric:
    description: str
    unit: str
    scope: str
    role: str
    value_type: str
    direction: str


@dataclass(frozen=True)
class SetupContract:
    environment_adapter_entrypoint: str
    environment_adapter_interface: str
    outcome_direction: str
    outcome_unit: str
    outcome_aggregation: str
    outcome_extraction: str
    trial_termination: str
    trial_horizon_unit: str
    hard_rules: tuple[str, ...]
    runtime_limits: tuple[str, ...]


def _setup_contract(value: Any) -> SetupContract:
    contract = _object(value, _SETUP_CONTRACT_FIELDS, "setup_contract")
    adapter = _object(
        contract["environment_adapter"],
        _SETUP_ADAPTER_FIELDS,
        "setup_contract.environment_adapter",
    )
    outcome = _object(
        contract["outcome"], _SETUP_OUTCOME_FIELDS, "setup_contract.outcome"
    )
    trial = _object(
        contract["trial"], _SETUP_TRIAL_FIELDS, "setup_contract.trial"
    )
    direction = outcome["direction"]
    if direction not in {"higher", "lower"}:
        raise ValidationError("setup_contract.outcome.direction is invalid")
    return SetupContract(
        environment_adapter_entrypoint=_text(
            adapter["entrypoint"], "setup_contract.environment_adapter.entrypoint"
        ),
        environment_adapter_interface=_text(
            adapter["interface"], "setup_contract.environment_adapter.interface"
        ),
        outcome_direction=direction,
        outcome_unit=_text(outcome["unit"], "setup_contract.outcome.unit"),
        outcome_aggregation=_text(
            outcome["aggregation"], "setup_contract.outcome.aggregation"
        ),
        outcome_extraction=_text(
            outcome["extraction"], "setup_contract.outcome.extraction"
        ),
        trial_termination=_text(
            trial["termination"], "setup_contract.trial.termination"
        ),
        trial_horizon_unit=_text(
            trial["horizon_unit"], "setup_contract.trial.horizon_unit"
        ),
        hard_rules=_string_tuple(contract["hard_rules"], "setup_contract.hard_rules"),
        runtime_limits=_string_tuple(
            contract["runtime_limits"], "setup_contract.runtime_limits"
        ),
    )


def _telemetry_metrics(
    value: Any,
    *,
    schema_version: int,
) -> dict[str, TelemetryMetric]:
    if schema_version < 3:
        names = _string_tuple(value, "public.telemetry")
        if names:
            raise ValidationError(
                "manifest-v1/v2 telemetry lacks semantic descriptors; use manifest-v3"
            )
        return {}
    if not isinstance(value, Mapping):
        raise ValidationError("public.telemetry must be an object")
    metrics: dict[str, TelemetryMetric] = {}
    for name, raw in value.items():
        if not isinstance(name, str) or not name:
            raise ValidationError("public.telemetry names must be non-empty strings")
        metric = _object(raw, _TELEMETRY_FIELDS, f"public.telemetry.{name}")
        scope = metric["scope"]
        role = metric["role"]
        value_type = metric["value_type"]
        direction = metric["direction"]
        if scope not in {"paired", "comparison"}:
            raise ValidationError(f"public.telemetry.{name}.scope is invalid")
        if role not in {
            "outcome",
            "mechanism",
            "safety",
            "implementation",
            "uncertainty",
        }:
            raise ValidationError(f"public.telemetry.{name}.role is invalid")
        if value_type not in {"number", "boolean"}:
            raise ValidationError(f"public.telemetry.{name}.value_type is invalid")
        if scope == "paired" and value_type != "number":
            raise ValidationError(
                f"public.telemetry.{name} paired metrics must be numeric"
            )
        if direction not in {"higher", "lower", "contextual"}:
            raise ValidationError(f"public.telemetry.{name}.direction is invalid")
        if value_type == "boolean" and direction != "contextual":
            raise ValidationError(
                f"public.telemetry.{name} boolean metrics must be contextual"
            )
        metrics[name] = TelemetryMetric(
            description=_text(
                metric["description"], f"public.telemetry.{name}.description"
            ),
            unit=_text(metric["unit"], f"public.telemetry.{name}.unit"),
            scope=scope,
            role=role,
            value_type=value_type,
            direction=direction,
        )
    return metrics


@dataclass(frozen=True)
class EvaluatorManifest:
    schema_version: int
    subject_command: tuple[str, ...]
    prepare_command: tuple[str, ...]
    calibrate_command: tuple[str, ...] | None
    score_command: tuple[str, ...]
    limits: ProcessLimits
    public_case_schema: Mapping[str, Any]
    subject_result_schema: Mapping[str, Any]
    public_statistic: str
    subject_interface: str
    public_telemetry: Mapping[str, TelemetryMetric]
    trial_meaning: str
    trial_dependence: str
    seed_to_case: str
    subject_visible_seed: bool
    score_statistic: str
    uncertainty_method: str
    positive_effect: str
    known_variation: str
    variation_mitigations: tuple[str, ...]
    suspect_trigger: str | None
    suspect_reason_codes: tuple[str, ...]
    calibration: CalibrationPolicy
    setup_contract: SetupContract | None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EvaluatorManifest:
        if not isinstance(value, Mapping):
            raise ValidationError("manifest must be an object")
        schema_version = value.get("schema_version")
        if schema_version not in (1, 2, 3, 4):
            raise ValidationError("manifest.schema_version must equal 1, 2, 3, or 4")
        fields = _FIELDS | ({"setup_contract"} if schema_version == 4 else set())
        root = _object(value, fields, "manifest")

        limits = _object(root["limits"], _LIMIT_FIELDS, "limits")
        schemas = _object(root["schemas"], _SCHEMA_FIELDS, "schemas")
        public = _object(root["public"], _PUBLIC_FIELDS, "public")
        trial = _object(root["trial"], _TRIAL_FIELDS, "trial")
        statistics = _object(root["statistics"], _STATISTIC_FIELDS, "statistics")
        variation = _object(root["variation"], _VARIATION_FIELDS, "variation")
        suspect = _object(root["suspect_test"], _SUSPECT_FIELDS, "suspect_test")
        calibration = _object(
            root["calibration"],
            (
                _CALIBRATION_V1_FIELDS if schema_version == 1 else _CALIBRATION_V2_FIELDS
            ),
            "calibration",
        )

        try:
            from jsonschema import Draft202012Validator
        except ImportError as error:
            raise StateError("jsonschema is required to validate evaluator manifests") from error
        for name, schema in schemas.items():
            if not isinstance(schema, Mapping):
                raise ValidationError(f"schemas.{name} must be a JSON Schema object")
            _reject_external_schema_refs(schema, f"schemas.{name}")
            try:
                Draft202012Validator.check_schema(schema)
            except Exception as error:
                raise ValidationError(f"schemas.{name} is not a valid JSON Schema") from error

        visible_seed = trial["subject_visible_seed"]
        if not isinstance(visible_seed, bool):
            raise ValidationError("trial.subject_visible_seed must be boolean")
        calibration_supported = calibration["supported"]
        if not isinstance(calibration_supported, bool):
            raise ValidationError("calibration.supported must be boolean")

        calibration_command = root["calibrate_command"]
        if calibration_supported:
            command = validate_command_template(
                calibration_command,
                label="calibrate_command",
                placeholders=("request", "response"),
            )
            policy = _text(calibration["policy"], "calibration.policy")
            if schema_version == 1:
                ceiling = _positive_integer(
                    calibration["ceiling"], "calibration.ceiling"
                )
                ladder = (ceiling,)
                diagnostic_name = None
                diagnostic_units = None
                diagnostic_maximum = None
            else:
                raw_ladder = calibration["ladder"]
                if (
                    not isinstance(raw_ladder, list)
                    or not raw_ladder
                    or any(
                        isinstance(count, bool)
                        or not isinstance(count, int)
                        or count <= 0
                        for count in raw_ladder
                    )
                    or raw_ladder != sorted(set(raw_ladder))
                ):
                    raise ValidationError(
                        "calibration.ladder must be strictly increasing positive integers"
                    )
                ladder = tuple(raw_ladder)
                ceiling = ladder[-1]
                diagnostic = _object(
                    calibration["diagnostic"],
                    _DIAGNOSTIC_FIELDS,
                    "calibration.diagnostic",
                )
                diagnostic_name = _text(
                    diagnostic["name"], "calibration.diagnostic.name"
                )
                diagnostic_units = _text(
                    diagnostic["units"], "calibration.diagnostic.units"
                )
                maximum = diagnostic["maximum"]
                if (
                    isinstance(maximum, bool)
                    or not isinstance(maximum, (int, float))
                    or not math.isfinite(maximum)
                    or maximum < 0
                ):
                    raise ValidationError(
                        "calibration.diagnostic.maximum must be a finite "
                        "non-negative number"
                    )
                diagnostic_maximum = float(maximum)
        else:
            if calibration_command is not None:
                raise ValidationError(
                    "calibrate_command must be null when calibration is unsupported"
                )
            if schema_version == 1:
                if (
                    calibration["policy"] is not None
                    or calibration["ceiling"] is not None
                ):
                    raise ValidationError(
                        "calibration policy and ceiling must be null when unsupported"
                    )
            elif (
                calibration["policy"] is not None
                or calibration["ladder"] is not None
                or calibration["diagnostic"] is not None
            ):
                raise ValidationError(
                    "calibration policy, ladder, and diagnostic must be null "
                    "when unsupported"
                )
            command = None
            policy = None
            ceiling = None
            ladder = ()
            diagnostic_name = None
            diagnostic_units = None
            diagnostic_maximum = None

        trigger = suspect["trigger"]
        reasons = _string_tuple(suspect["reason_codes"], "suspect_test.reason_codes")
        if trigger is None:
            if reasons:
                raise ValidationError("suspect reason codes require a trigger")
        else:
            trigger = _text(trigger, "suspect_test.trigger")
            if not reasons:
                raise ValidationError("suspect trigger requires at least one reason code")

        return cls(
            schema_version=schema_version,
            subject_command=validate_command_template(
                root["subject_command"],
                label="subject_command",
                placeholders=("input", "output"),
            ),
            prepare_command=validate_command_template(
                root["prepare_command"],
                label="prepare_command",
                placeholders=("request", "response"),
            ),
            calibrate_command=command,
            score_command=validate_command_template(
                root["score_command"],
                label="score_command",
                placeholders=("request", "response"),
            ),
            limits=ProcessLimits(
                timeout_seconds=_positive_integer(
                    limits["timeout_seconds"], "limits.timeout_seconds"
                ),
                max_output_bytes=_positive_integer(
                    limits["max_output_bytes"], "limits.max_output_bytes"
                ),
            ),
            public_case_schema=dict(schemas["public_case"]),
            subject_result_schema=dict(schemas["subject_result"]),
            public_statistic=_text(public["statistic"], "public.statistic"),
            subject_interface=_text(public["subject_interface"], "public.subject_interface"),
            public_telemetry=_telemetry_metrics(
                public["telemetry"], schema_version=schema_version
            ),
            trial_meaning=_text(trial["meaning"], "trial.meaning"),
            trial_dependence=_text(trial["dependence"], "trial.dependence"),
            seed_to_case=_text(trial["seed_to_case"], "trial.seed_to_case"),
            subject_visible_seed=visible_seed,
            score_statistic=_text(statistics["score"], "statistics.score"),
            uncertainty_method=_text(
                statistics["uncertainty"], "statistics.uncertainty"
            ),
            positive_effect=_text(
                statistics["positive_effect"], "statistics.positive_effect"
            ),
            known_variation=_text(variation["known"], "variation.known"),
            variation_mitigations=_string_tuple(
                variation["mitigations"], "variation.mitigations"
            ),
            suspect_trigger=trigger,
            suspect_reason_codes=reasons,
            calibration=CalibrationPolicy(
                calibration_supported,
                policy,
                ceiling,
                ladder,
                diagnostic_name,
                diagnostic_units,
                diagnostic_maximum,
                schema_version >= 2 and calibration_supported,
            ),
            setup_contract=(
                _setup_contract(root["setup_contract"])
                if schema_version == 4
                else None
            ),
        )

    def validate_trial_setting(self, trials: str | int) -> None:
        if trials == "auto":
            if not self.calibration.supported:
                raise ValidationError("task requests auto trials but evaluator has no calibration")
            return
        if isinstance(trials, bool) or not isinstance(trials, int) or trials <= 0:
            raise ValidationError("trials must be 'auto' or a positive integer")
        if self.calibration.ceiling is not None and trials > self.calibration.ceiling:
            raise ValidationError("fixed trial count exceeds the approved safety ceiling")
