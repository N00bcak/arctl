from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arctl.approval import confirm_approval, preview_approval
from arctl.errors import StateError
from arctl.experiment import start_experiment
from arctl.git import resolve_commit
from arctl.models import TaskConfig
from arctl.operations import inspect_experiment, task_report, task_status
from arctl.registry import LocatedTask
from arctl.runner import _default_research_command, _run_research, run_task
from arctl.taskio import load_manifest

from .helpers import valid_task
from .test_comparison_run_integration import _EVALUATOR, _SUBJECT
from .test_manifest import valid_manifest


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class RunnerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.subject = self.root / "subject"
        self.evaluator = self.root / "evaluator"
        for repository in (self.subject, self.evaluator):
            repository.mkdir()
            git(repository, "init", "-q")
            git(repository, "config", "user.name", "arctl tests")
            git(repository, "config", "user.email", "tests@arctl.invalid")
        (self.subject / "subject.py").write_text(_SUBJECT.replace("BIAS", "0"))
        git(self.subject, "add", ".")
        git(self.subject, "commit", "-qm", "champion")
        self.original_champion = resolve_commit(self.subject, "HEAD")

        (self.evaluator / "evaluator.py").write_text(_EVALUATOR)
        (self.evaluator / "evaluator.manifest.json").write_text(
            json.dumps(valid_manifest(), sort_keys=True, separators=(",", ":"))
        )
        git(self.evaluator, "add", ".")
        git(self.evaluator, "commit", "-qm", "evaluator")
        evaluator_commit = resolve_commit(self.evaluator, "HEAD")

        self.task_directory = self.root / "data" / "tasks" / "demo"
        self.task_directory.mkdir(parents=True)
        task_file = self.task_directory / "task.yaml"
        task_file.write_text("approved test task\n")
        raw = valid_task()
        raw.update(
            {
                "repo": str(self.subject),
                "editable_paths": ["subject.py"],
                "denied_paths": [".git/**"],
                "public_checks": [
                    [
                        "python3",
                        "-c",
                        "import ast; ast.parse(open('subject.py').read())",
                    ]
                ],
                "public_probe": ["python3", "-c", "print('public')"],
                "evaluator": {
                    "repo": str(self.evaluator),
                    "commit": evaluator_commit,
                },
                "trials": 4,
                "max_experiments": 1,
            }
        )
        self.config = TaskConfig.from_mapping(raw)
        preview = preview_approval(task_file, self.config)
        confirm_approval(
            self.task_directory,
            self.config,
            preview,
            preview.confirmation_token,
        )
        self.task = LocatedTask(self.task_directory, self.config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def unconfined_public(command, _cwd, _output):
        return command

    @staticmethod
    def unconfined_comparison(command, _cwd, _reads, _writes, _profile):
        return command

    @staticmethod
    def research_command(worktree: Path, scratch: Path, _schema: Path, _prompt: str):
        script = """\
import json
import sys
from pathlib import Path

worktree, scratch = map(Path, sys.argv[1:])
subject = worktree / "subject.py"
subject.write_text(subject.read_text().replace("+ 0", "+ 1"))
(scratch / "request.public.json").write_text(json.dumps({
    "schema_version": 1,
    "claim": "Add one point to each valid result.",
    "mechanism": "Increase the subject score by one.",
    "expected_effect": "The paired score difference is positive.",
    "expected_telemetry": {},
    "falsifiers": ["The paired effect is not positive."],
}))
"""
        return ("python3", "-c", script, str(worktree), str(scratch))

    def test_runs_fresh_research_through_promotion_and_publication(self) -> None:
        prompts: list[str] = []
        events: list[dict] = []

        def research(worktree: Path, scratch: Path, _schema: Path, prompt: str):
            prompts.append(prompt)
            return self.research_command(worktree, scratch, _schema, prompt)

        outcome = run_task(
            self.task,
            max_experiments=1,
            research_command_builder=research,
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
            progress=events.append,
        )

        self.assertFalse(outcome.stopped)
        self.assertEqual(len(outcome.results), 1)
        self.assertEqual(outcome.results[0]["decision"], "ACCEPT")
        candidate = outcome.results[0]["candidate"]
        self.assertEqual(
            resolve_commit(self.subject, "refs/arctl/demo/champion"),
            candidate,
        )
        self.assertNotEqual(candidate, self.original_champion)
        experiment = self.task_directory / "experiments" / "000001"
        self.assertTrue((experiment / "published").is_file())
        self.assertTrue(
            (experiment / "comparisons" / "primary" / "evidence.private.json").is_file()
        )
        self.assertFalse(
            (experiment / "comparisons" / "suspect").exists()
        )
        self.assertEqual(len(prompts), 1)
        self.assertNotIn(str(self.evaluator), prompts[0])
        self.assertNotIn("master_seed", prompts[0])
        self.assertFalse(
            (self.task_directory / "worktrees" / "000001-research").exists()
        )
        self.assertFalse(
            (self.task_directory / "worktrees" / "000001-candidate").exists()
        )
        self.assertEqual(
            [event["event"] for event in events if event["event"] != "stage"],
            [
                "ready",
                "experiment",
                "research",
                "candidate",
                "public_checks",
                "public_checks_complete",
                "comparison",
                "result",
                "complete",
            ],
        )
        self.assertEqual(
            [
                (event["stage"], event["status"])
                for event in events
                if event["event"] == "stage"
            ],
            [
                ("prepare", "started"),
                ("prepare", "complete"),
                ("subject", "started"),
                ("subject", "complete"),
                ("subject", "started"),
                ("subject", "complete"),
                ("score", "started"),
                ("score", "complete"),
                ("validate", "started"),
                ("validate", "complete"),
            ],
        )
        self.assertTrue(
            (
                self.task_directory
                / "reports"
                / "experiments"
                / "000001"
                / "README.md"
            ).is_file()
        )
        dossier = (
            self.task_directory / "reports" / "experiments" / "000001"
        )
        shutil.rmtree(dossier)
        reported = task_report(self.task)
        self.assertTrue((dossier / "README.md").is_file())
        self.assertEqual(
            reported["results"][0]["dossier_path"],
            str(dossier / "README.md"),
        )

        exhausted = run_task(
            self.task,
            max_experiments=1,
            research_command_builder=lambda *_: self.fail(
                "task-wide experiment ceiling was ignored"
            ),
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )
        self.assertEqual(exhausted.results, ())

    def test_default_research_receives_approved_public_runtime_paths(self) -> None:
        experiment, _ = start_experiment(self.task_directory, self.original_champion)
        runtime = self.root / "approved-runtime"
        runtime.mkdir()
        captured: dict = {}

        def build_research_command(**arguments):
            captured.update(arguments)
            return ("true",)

        def complete_research(*_arguments, **_keywords):
            scratch = experiment / "research"
            (scratch / "request.public.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "claim": "Use the approved public runtime.",
                        "mechanism": "Run public development tools.",
                        "expected_effect": "Improve the score.",
                        "expected_telemetry": {},
                        "falsifiers": ["The effect is not positive."],
                    }
                )
            )
            return {"return_code": 0}

        with (
            mock.patch(
                "arctl.runner.command_runtime_read_paths",
                return_value=(runtime,),
            ),
            mock.patch(
                "arctl.runner.research_command",
                side_effect=build_research_command,
            ),
            mock.patch(
                "arctl.runner.run_or_load_once",
                side_effect=complete_research,
            ),
        ):
            _run_research(
                self.task,
                experiment,
                self.subject,
                load_manifest(self.task_directory / "evaluator.manifest.json")[0],
                command_builder=_default_research_command,
                stop_path=self.task_directory / "stop.requested",
            )

        self.assertEqual(captured["read_paths"], (runtime,))

    def test_resumes_finalizing_experiment_without_research_or_evaluation_reruns(
        self,
    ) -> None:
        from arctl.runner import publish_final_result as real_publish

        with mock.patch(
            "arctl.runner.publish_final_result",
            side_effect=RuntimeError("simulated controller crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                run_task(
                    self.task,
                    max_experiments=1,
                    research_command_builder=self.research_command,
                    public_check_command_builder=self.unconfined_public,
                    comparison_command_builder=self.unconfined_comparison,
                )
        experiment = self.task_directory / "experiments" / "000001"
        process_starts = sorted(experiment.rglob("started.json"))
        timestamps = {path: path.stat().st_mtime_ns for path in process_starts}

        with mock.patch(
            "arctl.runner.publish_final_result",
            wraps=real_publish,
        ) as publish:
            outcome = run_task(
                self.task,
                max_experiments=1,
                research_command_builder=lambda *_: self.fail(
                    "research session was repeated"
                ),
                public_check_command_builder=self.unconfined_public,
                comparison_command_builder=self.unconfined_comparison,
            )

        self.assertEqual(outcome.results[0]["decision"], "ACCEPT")
        publish.assert_called_once()
        self.assertEqual(
            {path: path.stat().st_mtime_ns for path in process_starts},
            timestamps,
        )

    def test_stop_before_start_creates_no_experiment(self) -> None:
        (self.task_directory / "stop.requested").write_text("{}")

        outcome = run_task(
            self.task,
            max_experiments=1,
            research_command_builder=lambda *_: ("false",),
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )

        self.assertEqual(outcome.results, ())
        self.assertTrue(outcome.stopped)
        self.assertFalse((self.task_directory / "stop.requested").exists())
        self.assertFalse((self.task_directory / "experiments").exists())

    def test_auto_calibration_runs_once_before_research_and_uses_fresh_seeds(
        self,
    ) -> None:
        auto_directory = self.root / "data" / "tasks" / "auto"
        auto_directory.mkdir()
        task_file = auto_directory / "task.yaml"
        task_file.write_text("approved automatic test task\n")
        raw = valid_task()
        raw.update(
            {
                "task_id": "auto",
                "repo": str(self.subject),
                "editable_paths": ["subject.py"],
                "denied_paths": [".git/**"],
                "public_checks": [
                    [
                        "python3",
                        "-c",
                        "import ast; ast.parse(open('subject.py').read())",
                    ]
                ],
                "public_probe": ["python3", "-c", "print('public')"],
                "evaluator": {
                    "repo": str(self.evaluator),
                    "commit": resolve_commit(self.evaluator, "HEAD"),
                },
                "trials": "auto",
                "max_experiments": 1,
            }
        )
        config = TaskConfig.from_mapping(raw)
        preview = preview_approval(task_file, config)
        confirm_approval(
            auto_directory,
            config,
            preview,
            preview.confirmation_token,
        )
        task = LocatedTask(auto_directory, config)

        outcome = run_task(
            task,
            max_experiments=1,
            research_command_builder=self.research_command,
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
            calibration_command_builder=self.unconfined_comparison,
        )

        self.assertEqual(outcome.results[0]["decision"], "ACCEPT")
        trial_count = json.loads((auto_directory / "trial-count.json").read_text())
        self.assertEqual(trial_count["trial_count"], 4)
        calibration = json.loads(
            (auto_directory / "calibration.private.json").read_text()
        )
        comparison = json.loads(
            (
                auto_directory
                / "experiments"
                / "000001"
                / "comparisons"
                / "primary"
                / "reservation.private.json"
            ).read_text()
        )
        self.assertTrue(
            set(calibration["request"]["trial_seeds"]).isdisjoint(
                comparison["trial_seeds"]
            )
        )

    def test_primary_trigger_runs_exactly_one_fresh_suspect_comparison(self) -> None:
        evaluator_source = _EVALUATOR.replace(
            '"suspect_test": {"required": False, "reason": None},',
            (
                '"suspect_test": {'
                '"required": request["kind"] == "primary", '
                '"reason": "timeout_shift" if request["kind"] == "primary" else None'
                "},"
            ),
        )
        (self.evaluator / "evaluator.py").write_text(evaluator_source)
        git(self.evaluator, "add", ".")
        git(self.evaluator, "commit", "-qm", "suspect-trigger evaluator")
        task_directory = self.root / "data" / "tasks" / "suspect"
        task_directory.mkdir()
        task_file = task_directory / "task.yaml"
        task_file.write_text("approved suspect test task\n")
        raw = valid_task()
        raw.update(
            {
                "task_id": "suspect",
                "repo": str(self.subject),
                "editable_paths": ["subject.py"],
                "denied_paths": [".git/**"],
                "public_checks": [
                    [
                        "python3",
                        "-c",
                        "import ast; ast.parse(open('subject.py').read())",
                    ]
                ],
                "public_probe": ["python3", "-c", "print('public')"],
                "evaluator": {
                    "repo": str(self.evaluator),
                    "commit": resolve_commit(self.evaluator, "HEAD"),
                },
                "trials": 4,
                "max_experiments": 1,
            }
        )
        config = TaskConfig.from_mapping(raw)
        preview = preview_approval(task_file, config)
        confirm_approval(
            task_directory,
            config,
            preview,
            preview.confirmation_token,
        )

        outcome = run_task(
            LocatedTask(task_directory, config),
            max_experiments=1,
            research_command_builder=self.research_command,
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )

        result = outcome.results[0]
        self.assertEqual(result["decision"], "ACCEPT")
        self.assertEqual(
            [item["kind"] for item in result["evaluation"]["comparisons"]],
            ["primary", "suspect"],
        )
        primary = json.loads(
            (
                task_directory
                / "experiments"
                / "000001"
                / "comparisons"
                / "primary"
                / "reservation.private.json"
            ).read_text()
        )
        suspect = json.loads(
            (
                task_directory
                / "experiments"
                / "000001"
                / "comparisons"
                / "suspect"
                / "reservation.private.json"
            ).read_text()
        )
        self.assertTrue(
            set(primary["trial_seeds"]).isdisjoint(suspect["trial_seeds"])
        )

    def test_stop_during_research_discards_unreserved_experiment(self) -> None:
        stop = self.task_directory / "stop.requested"

        def research(_worktree, _scratch, _schema, _prompt):
            script = (
                "import pathlib,sys,time;"
                "pathlib.Path(sys.argv[1]).write_text('{}');"
                "time.sleep(30)"
            )
            return ("python3", "-c", script, str(stop))

        outcome = run_task(
            self.task,
            max_experiments=1,
            research_command_builder=research,
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )

        self.assertEqual(outcome.results, ())
        self.assertTrue(outcome.stopped)
        self.assertEqual(list((self.task_directory / "experiments").iterdir()), [])
        self.assertFalse(
            (self.task_directory / "worktrees" / "000001-research").exists()
        )

    def test_failed_research_is_inspectable_and_next_run_retries_cleanly(self) -> None:
        with self.assertRaisesRegex(StateError, "exited unsuccessfully"):
            run_task(
                self.task,
                max_experiments=1,
                research_command_builder=lambda *_: ("false",),
                public_check_command_builder=self.unconfined_public,
                comparison_command_builder=self.unconfined_comparison,
            )
        experiments = self.task_directory / "experiments"
        experiment = experiments / "000001"
        self.assertTrue((experiment / "research.failure.json").is_file())
        self.assertTrue((experiment / "process" / "research" / "result.json").is_file())
        self.assertEqual(task_status(self.task)["state"], "RESEARCH_FAILED")
        artifacts = {
            artifact["path"]: artifact["visibility"]
            for artifact in inspect_experiment(self.task, 1)["artifacts"]
        }
        self.assertEqual(artifacts["research.failure.json"], "private")
        self.assertEqual(
            artifacts["process/research/stderr.bin"],
            "private",
        )

        outcome = run_task(
            self.task,
            max_experiments=1,
            research_command_builder=self.research_command,
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )
        self.assertEqual(outcome.results[0]["experiment_id"], 1)
        self.assertFalse((experiment / "research.failure.json").exists())

    def test_public_check_sandbox_failure_is_inspectable_and_retryable(self) -> None:
        with mock.patch(
            "arctl.experiment.sandbox_command",
            return_value=("false",),
        ):
            with self.assertRaisesRegex(StateError, "sandbox did not start"):
                run_task(
                    self.task,
                    max_experiments=1,
                    research_command_builder=self.research_command,
                    comparison_command_builder=self.unconfined_comparison,
                )
        experiment = self.task_directory / "experiments" / "000001"
        self.assertTrue((experiment / "public-check.failure.json").is_file())
        self.assertEqual(task_status(self.task)["state"], "PUBLIC_CHECK_FAILED")
        artifacts = {
            artifact["path"]: artifact["visibility"]
            for artifact in inspect_experiment(self.task, 1)["artifacts"]
        }
        self.assertEqual(artifacts["public-check.failure.json"], "private")
        self.assertEqual(
            artifacts["process/public-check-0001/stderr.bin"],
            "private",
        )

        outcome = run_task(
            self.task,
            max_experiments=1,
            research_command_builder=lambda *_: self.fail(
                "research session was repeated"
            ),
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )

        self.assertEqual(outcome.results[0]["experiment_id"], 1)
        self.assertFalse((experiment / "public-check.failure.json").exists())

    def test_stop_after_reservation_publishes_invalid(self) -> None:
        stop = self.task_directory / "stop.requested"
        first = True

        def stop_comparison(command, _cwd, _reads, _writes, _profile):
            nonlocal first
            if first:
                first = False
                script = (
                    "import pathlib,sys,time;"
                    "pathlib.Path(sys.argv[1]).write_text('{}');"
                    "time.sleep(30)"
                )
                return ("python3", "-c", script, str(stop))
            return command

        outcome = run_task(
            self.task,
            max_experiments=1,
            research_command_builder=self.research_command,
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=stop_comparison,
        )

        self.assertTrue(outcome.stopped)
        self.assertEqual(outcome.results[0]["decision"], "INVALID")
        self.assertEqual(outcome.results[0]["failure"], "system_execution")
        self.assertEqual(
            resolve_commit(self.subject, "refs/arctl/demo/champion"),
            self.original_champion,
        )
        experiment = self.task_directory / "experiments" / "000001"
        self.assertTrue((experiment / "published").is_file())

    def test_stop_recovers_crashed_reserved_comparison_as_invalid(self) -> None:
        with mock.patch(
            "arctl.runner.run_comparison",
            side_effect=RuntimeError("simulated abrupt controller exit"),
        ):
            with self.assertRaisesRegex(RuntimeError, "abrupt"):
                run_task(
                    self.task,
                    max_experiments=1,
                    research_command_builder=self.research_command,
                    public_check_command_builder=self.unconfined_public,
                    comparison_command_builder=self.unconfined_comparison,
                )
        experiment = self.task_directory / "experiments" / "000001"
        self.assertEqual(
            json.loads((experiment / "experiment.json").read_text())["state"],
            "PRIMARY_RESERVED",
        )
        (self.task_directory / "stop.requested").write_text("{}")

        outcome = run_task(
            self.task,
            max_experiments=1,
            research_command_builder=lambda *_: self.fail(
                "research session was repeated"
            ),
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )

        self.assertTrue(outcome.stopped)
        self.assertEqual(outcome.results[0]["decision"], "INVALID")
        self.assertTrue((experiment / "published").is_file())
        self.assertFalse((self.task_directory / "stop.requested").exists())

    def test_stop_preserves_valid_primary_saved_just_before_crash(self) -> None:
        with mock.patch(
            "arctl.runner.save_comparison_result",
            side_effect=RuntimeError("crash after evidence"),
        ):
            with self.assertRaisesRegex(RuntimeError, "after evidence"):
                run_task(
                    self.task,
                    max_experiments=1,
                    research_command_builder=self.research_command,
                    public_check_command_builder=self.unconfined_public,
                    comparison_command_builder=self.unconfined_comparison,
                )
        (self.task_directory / "stop.requested").write_text("{}")

        outcome = run_task(
            self.task,
            max_experiments=1,
            research_command_builder=self.research_command,
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )

        self.assertEqual(outcome.results[0]["decision"], "INVALID")
        comparisons = outcome.results[0]["evaluation"]["comparisons"]
        self.assertEqual(len(comparisons), 1)
        self.assertEqual(comparisons[0]["effect_estimate"], 1)
