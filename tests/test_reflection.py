from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arctl.errors import StateError
from arctl.manifest import EvaluatorManifest
from arctl.models import TaskConfig
from arctl.reflection import run_reflection

from .helpers import valid_task
from .test_manifest import valid_manifest


def assessment(metrics: list[str]) -> dict:
    return {
        "schema_version": 1,
        "summary": "The aggregate result is uncertain and the mechanism remains unproven.",
        "metric_assessments": [
            {
                "metric": name,
                "finding": "inconclusive",
                "rationale": "The metric does not isolate the proposed mechanism.",
            }
            for name in metrics
        ],
        "mechanism": {
            "status": "not_demonstrated",
            "evidence": [],
            "missing_evidence": ["Direct activation telemetry is absent."],
        },
        "implementation": {
            "status": "activation_unclear",
            "evidence": [],
            "concerns": ["Behavioral divergence is not measured."],
        },
        "next_action": {
            "kind": "revisit_after_better_evidence",
            "rationale": "The positive estimate is not precise enough to promote.",
            "test": "Add an activation diagnostic before refining the change.",
        },
    }


class ReflectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.experiment = self.root / "task" / "experiments" / "000001"
        self.candidate = self.root / "candidate"
        self.champion = self.root / "champion"
        for path in (self.experiment, self.candidate, self.champion):
            path.mkdir(parents=True)
        raw_task = valid_task()
        raw_task["repo"] = str(self.root / "subject")
        self.task = TaskConfig.from_mapping(raw_task)
        self.manifest = EvaluatorManifest.from_mapping(
            valid_manifest(telemetry=True)
        )
        self.request = {
            "claim": "Reduce errors.",
            "mechanism": "Prefer safe actions.",
            "expected_effect": "Increase the primary score.",
            "expected_telemetry": {"errors": "decrease"},
            "falsifiers": ["Errors do not decrease."],
        }
        self.result = {
            "experiment_id": 1,
            "decision": "ARCHIVE",
            "evaluation": {
                "statistic": "expected score",
                "comparisons": [
                    {
                        "kind": "primary",
                        "trials": 64,
                        "effect_estimate": 0.2,
                        "one_sided_lower_bound": -0.5,
                        "suspect_test_required": False,
                        "suspect_test_reason": None,
                    }
                ],
            },
            "telemetry": {
                "errors": {"champion": 3.0, "candidate": 2.5, "delta": -0.5}
            },
            "constraints": {"tests": "PASS"},
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def builder(value: dict):
        def command(_worktree, scratch, _schema, _prompt):
            script = (
                "import pathlib,sys;"
                "pathlib.Path(sys.argv[1]).write_text(sys.argv[2])"
            )
            return (
                "python3",
                "-c",
                script,
                str(scratch / "assessment.public.json"),
                json.dumps(value),
            )

        return command

    def reflect(self, *, manifest=None, command_builder=None):
        return run_reflection(
            task=self.task,
            experiment=self.experiment,
            manifest=manifest or self.manifest,
            request=self.request,
            result=self.result,
            candidate_worktree=self.candidate,
            champion_worktree=self.champion,
            stop_path=self.root / "stop",
            command_builder=command_builder,
        )

    def test_saves_grounded_assessment_and_uncertainty_margin(self) -> None:
        value = self.reflect(command_builder=self.builder(assessment(["errors"])))

        self.assertEqual(value["status"], "COMPLETE")
        self.assertAlmostEqual(value["basis"]["uncertainty_margin"], 0.7)
        self.assertEqual(
            value["assessment"]["next_action"]["kind"],
            "revisit_after_better_evidence",
        )

    def test_failure_is_preserved_and_a_later_attempt_can_recover(self) -> None:
        with self.assertRaisesRegex(StateError, "post-trial reflection failed"):
            self.reflect(command_builder=self.builder(assessment([])))
        self.assertTrue(
            (
                self.experiment
                / "reflection"
                / "attempts"
                / "0001"
                / "reflection.failure.json"
            ).is_file()
        )

        recovered = self.reflect(
            command_builder=self.builder(assessment(["errors"]))
        )
        self.assertEqual(recovered["status"], "COMPLETE")
        self.assertTrue(
            (self.experiment / "reflection" / "attempts" / "0002").is_dir()
        )

    def test_empty_telemetry_contract_skips_model_with_warning(self) -> None:
        manifest = EvaluatorManifest.from_mapping(valid_manifest())
        called = False

        def forbidden(*_args):
            nonlocal called
            called = True
            return ("false",)

        value = self.reflect(manifest=manifest, command_builder=forbidden)
        self.assertEqual(value["status"], "SKIPPED_NO_TELEMETRY")
        self.assertFalse(called)
