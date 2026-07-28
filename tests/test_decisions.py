from __future__ import annotations

import unittest

from arctl.decisions import Decision, decide, failure_decision
from arctl.models import Evidence

from .helpers import valid_evidence


def evidence(**changes: object) -> Evidence:
    raw = valid_evidence(**changes)  # type: ignore[arg-type]
    return Evidence.from_mapping(
        raw,
        expected_kind=raw["kind"],
        expected_trial_count=128,
        allowed_suspect_reasons=("distribution_shift",),
    )


class DecisionTests(unittest.TestCase):
    def test_decision_table(self) -> None:
        cases = (
            ({}, Decision.ACCEPT),
            ({"lower": 0.0}, Decision.ARCHIVE),
            ({"lower": -0.01}, Decision.ARCHIVE),
            ({"estimate": 0.0, "lower": 0.0}, Decision.REJECT),
            ({"estimate": -0.1, "lower": -0.2}, Decision.REJECT),
            ({"hard_rules_pass": False}, Decision.REJECT),
        )
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.assertEqual(decide(evidence(**changes)), expected)

    def test_failure_source_controls_reject_vs_invalid(self) -> None:
        self.assertEqual(failure_decision("candidate"), Decision.REJECT)
        for source in (
            "champion",
            "evaluator",
            "evidence",
            "sandbox",
            "controller",
            "host",
            "stop",
            "crash",
        ):
            with self.subTest(source=source):
                self.assertEqual(failure_decision(source), Decision.INVALID)

    def test_primary_suspect_request_is_provisional(self) -> None:
        result = decide(
            evidence(
                suspect_required=True,
                suspect_reason="distribution_shift",
            )
        )
        self.assertEqual(result, Decision.PROVISIONAL)

    def test_suspect_accept_is_final(self) -> None:
        self.assertEqual(decide(evidence(kind="suspect")), Decision.ACCEPT)

    def test_exact_lower_bound_equals_estimate(self) -> None:
        self.assertEqual(decide(evidence(estimate=0.2, lower=0.2)), Decision.ACCEPT)
