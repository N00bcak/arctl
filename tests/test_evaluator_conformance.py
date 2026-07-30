from __future__ import annotations

import json
import math
import statistics
import unittest
from pathlib import Path

from arctl.decisions import Decision, decide
from arctl.manifest import EvaluatorManifest
from arctl.models import Evidence


def _evidence(estimate: float, lower: float, trial_count: int) -> Evidence:
    return Evidence.from_mapping(
        {
            "schema_version": 1,
            "kind": "primary",
            "trial_count": trial_count,
            "hard_rules_pass": True,
            "comparison": {
                "effect_estimate": estimate,
                "one_sided_lower_bound": lower,
            },
            "suspect_test": {"required": False, "reason": None},
            "telemetry": {},
        },
        expected_kind="primary",
        expected_trial_count=trial_count,
    )


def _binomial_tail(successes: int, trials: int, probability: float) -> float:
    return sum(
        math.comb(trials, count)
        * probability**count
        * (1.0 - probability) ** (trials - count)
        for count in range(successes, trials + 1)
    )


def _binary_lower_bound(wins: int, losses: int, alpha: float = 0.05) -> float:
    trials = wins + losses
    if wins == 0:
        return -1.0
    low, high = 0.0, wins / trials
    for _ in range(80):
        midpoint = (low + high) / 2
        if _binomial_tail(wins, trials, midpoint) < alpha:
            low = midpoint
        else:
            high = midpoint
    return 2 * high - 1


def _median_lower_bound(differences: list[float], alpha: float = 0.05) -> float:
    ordered = sorted(differences)
    rank = 0
    for candidate in range(len(ordered)):
        tail = sum(
            math.comb(len(ordered), count)
            for count in range(candidate + 1)
        ) / (2 ** len(ordered))
        if tail <= alpha:
            rank = candidate
        else:
            break
    return ordered[rank]


class EvaluatorConformanceTests(unittest.TestCase):
    def test_reference_manifests_cover_mean_binary_and_non_mean_statistics(self) -> None:
        root = Path(__file__).parent / "fixtures" / "evaluators"
        manifests = {
            path.stem: EvaluatorManifest.from_mapping(json.loads(path.read_text()))
            for path in sorted(root.glob("*.json"))
        }
        self.assertEqual(set(manifests), {"mean", "binary", "median"})
        self.assertIn("Clopper-Pearson", manifests["binary"].uncertainty_method)
        self.assertIn("order-statistic", manifests["median"].uncertainty_method)

    def test_reference_statistics_flow_through_common_evidence_and_decision(self) -> None:
        mean_differences = [1.0] * 8
        mean = statistics.mean(mean_differences)
        mean_lower = mean

        wins, losses = 8, 0
        binary = 2 * wins / (wins + losses) - 1
        binary_lower = _binary_lower_bound(wins, losses)

        median_differences = [0.5, 0.7, 1.0, 1.1, 1.2, 1.4, 1.5, 2.0]
        median = statistics.median(median_differences)
        median_lower = _median_lower_bound(median_differences)

        for estimate, lower in (
            (mean, mean_lower),
            (binary, binary_lower),
            (median, median_lower),
        ):
            with self.subTest(estimate=estimate, lower=lower):
                evidence = _evidence(estimate, lower, 8)
                self.assertEqual(decide(evidence), Decision.ACCEPT)

    def test_common_decision_states_remain_statistic_agnostic(self) -> None:
        cases = (
            (_evidence(1.0, 0.1, 8), Decision.ACCEPT),
            (_evidence(1.0, -0.1, 8), Decision.ARCHIVE),
            (_evidence(-0.1, -1.0, 8), Decision.REJECT),
        )
        for evidence, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(decide(evidence), expected)
