from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from arctl.calibration import _pilot_selection, calibrate_trial_count
from arctl.errors import StateError
from arctl.manifest import EvaluatorManifest
from arctl.models import TaskConfig
from arctl.operations import task_status
from arctl.registry import LocatedTask

from .helpers import valid_task
from .test_manifest import valid_manifest

_CALIBRATOR = """\
import json
import sys
from pathlib import Path

request = json.loads(Path(sys.argv[1]).read_text())
Path(sys.argv[2]).write_text(json.dumps({
    "schema_version": 1,
    "operation": "calibrate",
    "champion": request["champion"],
    "evaluator": request["evaluator"],
    "manifest": request["manifest"],
    "policy": request["policy"],
    "recommended_trial_count": RECOMMENDATION,
    "criterion_met": True,
    "evidence": {"policy": "test precision target met"},
}))
"""


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class CalibrationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.subject = self.root / "subject"
        self.evaluator = self.root / "evaluator"
        for repo in (self.subject, self.evaluator):
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.name", "arctl tests")
            git(repo, "config", "user.email", "tests@arctl.invalid")
        (self.subject / "model.py").write_text("value = 1\n")
        git(self.subject, "add", ".")
        git(self.subject, "commit", "-qm", "champion")
        self.champion = git(self.subject, "rev-parse", "HEAD")
        git(
            self.subject,
            "update-ref",
            "refs/arctl/demo/champion",
            self.champion,
        )
        (self.evaluator / "evaluator.py").write_text(
            _CALIBRATOR.replace("RECOMMENDATION", "4")
        )
        git(self.evaluator, "add", ".")
        git(self.evaluator, "commit", "-qm", "evaluator")
        self.evaluator_commit = git(self.evaluator, "rev-parse", "HEAD")
        raw_task = valid_task()
        raw_task["repo"] = str(self.subject)
        raw_task["evaluator"] = {
            "repo": str(self.evaluator),
            "commit": self.evaluator_commit,
        }
        self.task = TaskConfig.from_mapping(raw_task)
        raw_manifest = json.dumps(
            valid_manifest(version=1), sort_keys=True, separators=(",", ":")
        ).encode()
        self.manifest_hash = hashlib.sha256(raw_manifest).hexdigest()
        self.manifest = EvaluatorManifest.from_mapping(json.loads(raw_manifest))
        self.task_directory = self.root / "task"
        self.task_directory.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def unconfined(command, _cwd, _reads, _writes, _profile):
        return command

    def calibrate(self) -> int:
        return calibrate_trial_count(
            self.task_directory,
            self.task,
            self.manifest,
            manifest_hash=self.manifest_hash,
            evaluator_commit=self.evaluator_commit,
            evaluator_directory=self.evaluator,
            stop_path=self.task_directory / "stop.requested",
            command_builder=self.unconfined,
        )

    def test_freezes_approved_recommendation_and_recovers_without_rerun(self) -> None:
        self.assertEqual(self.calibrate(), 4)
        started = self.task_directory / "calibration" / "process" / "started.json"
        timestamp = started.stat().st_mtime_ns

        def changed_builder(*_arguments):
            raise AssertionError("completed calibration rebuilt its command")

        self.assertEqual(
            calibrate_trial_count(
                self.task_directory,
                self.task,
                self.manifest,
                manifest_hash=self.manifest_hash,
                evaluator_commit=self.evaluator_commit,
                evaluator_directory=self.evaluator,
                stop_path=self.task_directory / "stop.requested",
                command_builder=changed_builder,
            ),
            4,
        )
        self.assertEqual(started.stat().st_mtime_ns, timestamp)
        record = json.loads(
            (self.task_directory / "calibration.private.json").read_text()
        )
        self.assertEqual(len(record["request"]["trial_seeds"]), 256)
        self.assertNotIn(
            "master_seed",
            json.loads((self.task_directory / "trial-count.json").read_text()),
        )

    def test_rejects_recommendation_above_approved_ceiling(self) -> None:
        (self.evaluator / "evaluator.py").write_text(
            _CALIBRATOR.replace("RECOMMENDATION", "257")
        )
        git(self.evaluator, "add", ".")
        git(self.evaluator, "commit", "-qm", "invalid recommendation")
        self.evaluator_commit = git(self.evaluator, "rev-parse", "HEAD")
        raw_task = valid_task()
        raw_task["repo"] = str(self.subject)
        raw_task["evaluator"] = {
            "repo": str(self.evaluator),
            "commit": self.evaluator_commit,
        }
        self.task = TaskConfig.from_mapping(raw_task)

        with self.assertRaisesRegex(StateError, "violates"):
            self.calibrate()
        self.assertFalse((self.task_directory / "trial-count.json").exists())
        (self.task_directory / "approval.json").write_text("{}")
        status = task_status(LocatedTask(self.task_directory, self.task))
        self.assertEqual(status["state"], "CALIBRATION_FAILED")
        self.assertEqual(status["calibration"], "failed")


class ControllerPilotSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = EvaluatorManifest.from_mapping(valid_manifest())
        self.request = {"champion": "c" * 40}

    def response(self, values: list[float]) -> dict:
        return {
            "schema_version": 2,
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
