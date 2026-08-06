from __future__ import annotations

import copy
import unittest

from arctl.decisions import Decision
from arctl.errors import StateError
from arctl.models import Evidence
from arctl.manifest import TelemetryMetric
from arctl.results import build_public_result, resolve_outcome, validate_public_result

from .helpers import valid_evidence


def parse(kind: str = "primary", **changes: object) -> Evidence:
    raw = valid_evidence(kind=kind, **changes)  # type: ignore[arg-type]
    return Evidence.from_mapping(
        raw,
        expected_kind=kind,  # type: ignore[arg-type]
        expected_trial_count=128,
        allowed_telemetry=(
            {
                "errors": TelemetryMetric(
                    "Mean errors", "errors per case", "paired", "safety", "number", "lower"
                )
            }
            if "telemetry" in changes
            else {}
        ),
        allowed_suspect_reasons=("timeout_shift",),
    )


class ExperimentOutcomeTests(unittest.TestCase):
    def test_primary_trigger_holds_the_candidate_provisional(self) -> None:
        primary = parse(
            suspect_required=True,
            suspect_reason="timeout_shift",
        )
        outcome = resolve_outcome(primary)
        self.assertEqual(outcome.decision, Decision.PROVISIONAL)
        self.assertFalse(outcome.final)
        self.assertFalse(outcome.may_promote)
        with self.assertRaisesRegex(StateError, "provisional"):
            self.publish(outcome)

    def test_suspect_result_becomes_final(self) -> None:
        primary = parse(
            suspect_required=True,
            suspect_reason="timeout_shift",
        )
        outcome = resolve_outcome(primary, parse(kind="suspect", lower=-0.01))
        self.assertEqual(outcome.decision, Decision.ARCHIVE)
        public = self.publish(outcome)
        self.assertEqual(len(public["evaluation"]["comparisons"]), 2)
        self.assertEqual(public["champion_after"], "a" * 40)

    def test_only_final_accept_changes_public_champion(self) -> None:
        outcome = resolve_outcome(
            parse(telemetry={"errors": {"champion": 3, "candidate": 2}})
        )
        self.assertTrue(outcome.may_promote)
        public = self.publish(outcome)
        self.assertEqual(public["champion_after"], "b" * 40)
        self.assertEqual(
            public["telemetry"],
            {"errors": {"champion": 3.0, "candidate": 2.0, "delta": -1.0}},
        )

    def test_rejects_unrequested_or_wrong_kind_suspect_evidence(self) -> None:
        with self.assertRaisesRegex(StateError, "without an approved"):
            resolve_outcome(parse(), parse(kind="suspect"))
        primary = parse(
            suspect_required=True,
            suspect_reason="timeout_shift",
        )
        with self.assertRaisesRegex(StateError, "wrong kind"):
            resolve_outcome(primary, parse())

    def test_revalidates_public_history_before_research_exposure(self) -> None:
        metric = TelemetryMetric(
            "Mean errors", "errors per case", "paired", "safety", "number", "lower"
        )
        public = self.publish(
            resolve_outcome(
                parse(telemetry={"errors": {"champion": 3, "candidate": 2}})
            )
        )
        validated = validate_public_result(
            public,
            allowed_telemetry={"errors": metric},
            allowed_suspect_reasons=("timeout_shift",),
            expected_statistic="expected score",
        )
        self.assertEqual(validated, public)

        mutations = (
            lambda value: value.__setitem__("private_seed", "secret"),
            lambda value: value["telemetry"].__setitem__("hidden_score", 7),
            lambda value: value["evaluation"].__setitem__("statistic", "injected"),
            lambda value: value.__setitem__("champion_after", "wrong"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                tampered = copy.deepcopy(public)
                mutate(tampered)
                with self.assertRaises(StateError):
                    validate_public_result(
                        tampered,
                        allowed_telemetry={"errors": metric},
                        allowed_suspect_reasons=("timeout_shift",),
                        expected_statistic="expected score",
                    )

    def test_precomparison_rejection_requires_no_declared_telemetry(self) -> None:
        metric = TelemetryMetric(
            "Mean errors", "errors per case", "paired", "safety", "number", "lower"
        )
        public = {
            "experiment_id": 7,
            "hypothesis": "Improve routing.",
            "champion_before": "a" * 40,
            "candidate": "b" * 40,
            "champion_after": "a" * 40,
            "decision": "REJECT",
            "evaluation": {"statistic": None, "comparisons": []},
            "constraints": {"tests": "FAIL"},
            "telemetry": {},
        }

        self.assertEqual(
            validate_public_result(
                public,
                allowed_telemetry={"errors": metric},
                allowed_suspect_reasons=(),
                expected_statistic="expected score",
            ),
            public,
        )

    def test_accepts_explained_execution_failure_and_legacy_failure(self) -> None:
        public = {
            "experiment_id": 7,
            "hypothesis": "Improve routing.",
            "champion_before": "a" * 40,
            "candidate": "b" * 40,
            "champion_after": "a" * 40,
            "decision": "REJECT",
            "evaluation": {"statistic": "expected score", "comparisons": []},
            "constraints": {"tests": "PASS"},
            "telemetry": {},
            "failure": "candidate_execution",
            "failure_detail": "Candidate exceeded the approved limit.",
        }
        arguments = {
            "allowed_telemetry": {},
            "allowed_suspect_reasons": (),
            "expected_statistic": "expected score",
        }

        self.assertEqual(validate_public_result(public, **arguments), public)
        legacy = {key: value for key, value in public.items() if key != "failure_detail"}
        self.assertEqual(validate_public_result(legacy, **arguments), legacy)

        invalid = {**public, "failure_detail": ""}
        with self.assertRaisesRegex(StateError, "failure detail"):
            validate_public_result(invalid, **arguments)

    def publish(self, outcome):
        return build_public_result(
            experiment_id=7,
            hypothesis="Improve routing.",
            champion_before="a" * 40,
            candidate="b" * 40,
            statistic="expected score",
            outcome=outcome,
            tests_pass=True,
        )
