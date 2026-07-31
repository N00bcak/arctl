from __future__ import annotations

import math
import unittest

from arctl.errors import ValidationError
from arctl.models import Evidence, ResearchRequest, TaskConfig

from .helpers import valid_evidence, valid_task


class TaskConfigTests(unittest.TestCase):
    def test_accepts_strict_task(self) -> None:
        task = TaskConfig.from_mapping(valid_task())
        self.assertEqual(task.task_id, "demo")
        self.assertEqual(task.trials, "auto")
        self.assertEqual(task.public_checks[0][0], "python3")

    def test_rejects_unknown_fields(self) -> None:
        raw = valid_task()
        raw["metric"] = "accuracy"
        with self.assertRaisesRegex(ValidationError, "extra=.*metric"):
            TaskConfig.from_mapping(raw)

    def test_strategy_model_defaults_and_strict_override(self) -> None:
        default = TaskConfig.from_mapping(valid_task())
        self.assertEqual(default.strategy_model, "gpt-5.6-sol")
        self.assertEqual(default.strategy_reasoning_effort, "high")

        raw = valid_task()
        raw["strategy"] = {"model": "custom-model", "reasoning_effort": "xhigh"}
        configured = TaskConfig.from_mapping(raw)
        self.assertEqual(configured.strategy_model, "custom-model")
        self.assertEqual(configured.strategy_reasoning_effort, "xhigh")

        raw["strategy"]["reasoning_effort"] = "extreme"
        with self.assertRaisesRegex(ValidationError, "reasoning_effort"):
            TaskConfig.from_mapping(raw)

    def test_rejects_shell_command_strings(self) -> None:
        raw = valid_task()
        raw["public_probe"] = "python3 tools/probe.py"
        with self.assertRaisesRegex(ValidationError, "argument-vector"):
            TaskConfig.from_mapping(raw)

    def test_rejects_boolean_trial_count(self) -> None:
        raw = valid_task()
        raw["trials"] = True
        with self.assertRaisesRegex(ValidationError, "trials"):
            TaskConfig.from_mapping(raw)

    def test_rejects_relative_repositories(self) -> None:
        raw = valid_task()
        raw["repo"] = "subject"
        with self.assertRaisesRegex(ValidationError, "absolute"):
            TaskConfig.from_mapping(raw)

    def test_rejects_task_id_path_and_ref_escapes(self) -> None:
        for task_id in ("../escape", "bad/name", ".hidden", "space name"):
            with self.subTest(task_id=task_id):
                raw = valid_task()
                raw["task_id"] = task_id
                with self.assertRaisesRegex(ValidationError, "task_id"):
                    TaskConfig.from_mapping(raw)


class EvidenceTests(unittest.TestCase):
    def parse(self, raw: dict, kind: str = "primary") -> Evidence:
        return Evidence.from_mapping(
            raw,
            expected_kind=kind,  # type: ignore[arg-type]
            expected_trial_count=128,
            allowed_suspect_reasons=("distribution_shift",),
        )

    def test_accepts_exact_valid_schema(self) -> None:
        evidence = self.parse(valid_evidence())
        self.assertEqual(evidence.effect_estimate, 0.037)

    def test_rejects_nonfinite_numbers(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValidationError, "finite"):
                    self.parse(valid_evidence(estimate=value))

    def test_rejects_lower_bound_above_estimate(self) -> None:
        with self.assertRaisesRegex(ValidationError, "must not exceed"):
            self.parse(valid_evidence(estimate=0.1, lower=0.2))

    def test_rejects_identity_and_trial_mismatches(self) -> None:
        raw = valid_evidence(kind="suspect")
        with self.assertRaisesRegex(ValidationError, "kind"):
            self.parse(raw)
        raw = valid_evidence()
        raw["trial_count"] = 127
        with self.assertRaisesRegex(ValidationError, "trial_count"):
            self.parse(raw)

    def test_rejects_unapproved_telemetry(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unapproved telemetry"):
            self.parse(valid_evidence(telemetry={"secret_metric": 1}))

    def test_primary_can_request_one_approved_suspect_test(self) -> None:
        evidence = self.parse(
            valid_evidence(
                suspect_required=True,
                suspect_reason="distribution_shift",
            )
        )
        self.assertTrue(evidence.suspect_required)

    def test_suspect_request_requires_an_otherwise_accepted_result(self) -> None:
        for changes in (
            {"lower": 0.0},
            {"estimate": 0.0, "lower": 0.0},
            {"hard_rules_pass": False},
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValidationError, "otherwise accepted"):
                    self.parse(
                        valid_evidence(
                            suspect_required=True,
                            suspect_reason="distribution_shift",
                            **changes,
                        )
                    )

    def test_suspect_cannot_request_recursion_or_emit_telemetry(self) -> None:
        raw = valid_evidence(
            kind="suspect",
            suspect_required=True,
            suspect_reason="distribution_shift",
        )
        with self.assertRaisesRegex(ValidationError, "cannot request"):
            self.parse(raw, "suspect")

    def test_rejects_reason_without_request(self) -> None:
        with self.assertRaisesRegex(ValidationError, "reason must be null"):
            self.parse(valid_evidence(suspect_reason="distribution_shift"))

    def test_public_telemetry_is_scalar_and_finite(self) -> None:
        evidence = Evidence.from_mapping(
            valid_evidence(telemetry={"count": 2.0, "passed": True, "note": None}),
            expected_kind="primary",
            expected_trial_count=128,
            allowed_telemetry=("count", "passed", "note"),
        )
        self.assertEqual(evidence.telemetry["count"], 2.0)
        for invalid in (math.nan, math.inf, "text", [], {}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    Evidence.from_mapping(
                        valid_evidence(telemetry={"count": invalid}),
                        expected_kind="primary",
                        expected_trial_count=128,
                        allowed_telemetry=("count",),
                    )


class ResearchRequestTests(unittest.TestCase):
    def valid(self) -> dict:
        return {
            "schema_version": 1,
            "claim": "Prefer recoverable routes.",
            "mechanism": "Penalize branches without retreat.",
            "expected_effect": "Complete more maps.",
            "expected_telemetry": {"dead_ends": "decrease"},
            "falsifiers": ["The paired effect is not positive."],
        }

    def test_accepts_hypothesis_with_allowlisted_telemetry(self) -> None:
        request = ResearchRequest.from_mapping(
            self.valid(),
            allowed_telemetry=("dead_ends",),
        )
        self.assertEqual(request.claim, "Prefer recoverable routes.")

    def test_rejects_unapproved_telemetry_and_empty_falsifiers(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unapproved telemetry"):
            ResearchRequest.from_mapping(self.valid(), allowed_telemetry=())
        raw = self.valid()
        raw["falsifiers"] = []
        with self.assertRaisesRegex(ValidationError, "must not be empty"):
            ResearchRequest.from_mapping(raw, allowed_telemetry=("dead_ends",))
