from __future__ import annotations

import unittest

from arctl.calibration import _pilot_selection
from arctl.errors import StateError
from arctl.manifest import EvaluatorManifest

from .test_manifest import valid_manifest


class ControllerPilotSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = EvaluatorManifest.from_mapping(valid_manifest())
        self.request = {"champion": "c" * 40}

    def response(self, values: list[float]) -> dict:
        return {
            "operation": "calibrate",
            "champion": self.request["champion"],
            "evaluator": "e" * 40,
            "manifest": "m" * 64,
            "policy": self.manifest.calibration.policy,
            "assessments": [
                {"trial_count": count, "diagnostic_value": value}
                for count, value in zip(
                    self.manifest.calibration.ladder, values, strict=True
                )
            ],
        }

    def select(self, values: list[float]):
        return _pilot_selection(
            self.response(values),
            request=self.request,
            manifest=self.manifest,
            evaluator_commit="e" * 40,
            manifest_hash="m" * 64,
        )

    def test_selects_smallest_stable_passing_suffix(self) -> None:
        count, summary = self.select([0.5, 2.0, 0.5, 0.4])
        self.assertEqual(count, 64)
        self.assertTrue(summary["criterion_met"])
        self.assertFalse(summary["ceiling_fallback"])

    def test_uses_ceiling_and_records_warning_when_target_is_unmet(self) -> None:
        count, summary = self.select([2.0, 1.5, 1.2, 1.1])
        self.assertEqual(count, 256)
        self.assertFalse(summary["criterion_met"])
        self.assertTrue(summary["ceiling_fallback"])

    def test_rejects_incomplete_or_nonfinite_assessments(self) -> None:
        incomplete = self.response([0.5, 0.4, 0.3, 0.2])
        incomplete["assessments"].pop()
        with self.assertRaises(StateError):
            _pilot_selection(
                incomplete,
                request=self.request,
                manifest=self.manifest,
                evaluator_commit="e" * 40,
                manifest_hash="m" * 64,
            )
        with self.assertRaises(StateError):
            self.select([0.5, 0.4, 0.3, float("nan")])
