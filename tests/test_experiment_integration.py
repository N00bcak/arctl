from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arctl.decisions import Decision
from arctl.errors import StateError, TransientDownstreamError
from arctl.experiment import (
    complete_reflection,
    freeze_candidate,
    load_experiment,
    mark_comparison_reserved,
    publish_candidate_rejection,
    publish_comparison_failure,
    publish_final_result,
    run_public_checks,
    save_comparison_result,
    start_experiment,
)
from arctl.git import create_detached_worktree, resolve_commit
from arctl.manifest import EvaluatorManifest
from arctl.models import Evidence, TaskConfig
from arctl.sandbox import command_runtime_read_paths

from .helpers import valid_evidence, valid_task
from .test_manifest import valid_manifest


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class ExperimentIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "subject"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "arctl tests")
        git(self.repo, "config", "user.email", "tests@arctl.invalid")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "model.py").write_text("score = 1\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "champion")
        self.champion = resolve_commit(self.repo, "HEAD")
        git(
            self.repo,
            "update-ref",
            "refs/arctl/demo/champion",
            self.champion,
        )
        self.task_directory = self.root / "data" / "tasks" / "demo"
        self.task_directory.mkdir(parents=True)
        raw_task = valid_task()
        raw_task["repo"] = str(self.repo)
        raw_task["public_checks"] = [
            ["python3", "-c", "from pathlib import Path; assert Path('src/model.py').is_file()"]
        ]
        raw_task["trials"] = 4
        self.task = TaskConfig.from_mapping(raw_task)
        self.manifest = EvaluatorManifest.from_mapping(valid_manifest())
        self.experiment_directory, self.record = start_experiment(
            self.task_directory,
            self.champion,
        )
        self.research_worktree = (
            self.task_directory / "worktrees" / "000001-research"
        )
        create_detached_worktree(
            self.repo,
            self.research_worktree,
            self.champion,
        )
        self.request = {
            "schema_version": 2,
            "strategy_behavior_id": "recoverable-routing",
            "claim": "Prefer recoverable routes.",
            "mechanism": "Penalize branches without retreat.",
            "viability": "The policy exposes a branch score.",
            "evidence_review": {"summary": "No prior evidence.", "citations": []},
            "expected_effect": "Complete more maps.",
            "expected_telemetry": {},
            "falsifiers": ["The paired effect is not positive."],
            "lineage": {"kind": "new", "prior_entry_id": None},
        }
        (self.experiment_directory / "request.public.json").write_text(
            json.dumps(self.request)
        )
        (self.research_worktree / "src" / "model.py").write_text("score = 2\n")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_transient_public_check_preserves_failure_and_uses_fresh_retry(self) -> None:
        calls = 0

        def command_builder(_command, _worktree, _output):
            nonlocal calls
            calls += 1
            if calls == 1:
                return (
                    "python3",
                    "-c",
                    "import sys; print('urllib.error.HTTPError: HTTP Error 503: Service Unavailable', file=sys.stderr); raise SystemExit(1)",
                )
            return ("true",)

        self.freeze()
        with self.assertRaises(TransientDownstreamError):
            run_public_checks(
                self.task_directory,
                self.experiment_directory,
                self.task,
                command_builder=command_builder,
            )
        first = self.experiment_directory / "process" / "public-check-0001"
        self.assertTrue((first / "stderr.bin").is_file())

        self.assertTrue(
            run_public_checks(
                self.task_directory,
                self.experiment_directory,
                self.task,
                command_builder=command_builder,
            )
        )
        retry = (
            self.experiment_directory
            / "process"
            / "public-check-0001-retry-0001"
        )
        self.assertTrue((retry / "result.json").is_file())
        self.assertTrue((first / "stderr.bin").is_file())

    def freeze(self):
        return freeze_candidate(
            self.experiment_directory,
            self.research_worktree,
            self.task,
            self.manifest,
        )

    def evidence(self, *, kind: str = "primary", **changes: object) -> Evidence:
        raw = valid_evidence(kind=kind, **changes)  # type: ignore[arg-type]
        raw["trial_count"] = 4
        return Evidence.from_mapping(
            raw,
            expected_kind=kind,  # type: ignore[arg-type]
            expected_trial_count=4,
            allowed_suspect_reasons=("timeout_shift",),
        )

    @staticmethod
    def unconfined(command, _cwd, _output):
        return command

    def test_freezes_checks_publishes_and_promotes_idempotently(self) -> None:
        frozen, request = self.freeze()
        self.assertEqual(frozen.state, "CANDIDATE_FROZEN")
        self.assertIsNotNone(frozen.candidate)
        self.assertEqual(
            git(self.repo, "show", "-s", "--format=%P", frozen.candidate),
            self.champion,
        )
        self.assertEqual(
            resolve_commit(self.repo, "refs/arctl/demo/candidates/000001"),
            frozen.candidate,
        )
        self.assertTrue(
            run_public_checks(
                self.task_directory,
                self.experiment_directory,
                self.task,
                command_builder=self.unconfined,
            )
        )
        mark_comparison_reserved(self.experiment_directory, kind="primary")
        primary = self.evidence()
        finalizing = save_comparison_result(self.experiment_directory, primary)
        self.assertEqual(finalizing.state, "FINALIZING")

        with mock.patch(
            "arctl.experiment.ensure_experiment_dossier",
            side_effect=StateError("derived report unavailable"),
        ):
            public = publish_final_result(
                self.task,
                self.experiment_directory,
                self.manifest,
                request,
                primary,
            )
        self.assertEqual(public["decision"], "ACCEPT")
        self.assertEqual(
            resolve_commit(self.repo, "refs/arctl/demo/champion"),
            frozen.candidate,
        )
        (self.experiment_directory / "reflection.public.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "SKIPPED_NO_TELEMETRY",
                    "warning": "No telemetry.",
                    "basis": {},
                    "assessment": None,
                }
            )
        )
        with mock.patch(
            "arctl.experiment.ensure_experiment_dossier",
            side_effect=StateError("derived report unavailable"),
        ):
            complete_reflection(self.task, self.experiment_directory, public)
        self.assertEqual(load_experiment(self.experiment_directory).state, "COMPLETE")
        self.assertTrue((self.experiment_directory / "published").exists())

        recovered = publish_final_result(
            self.task,
            self.experiment_directory,
            self.manifest,
            request,
            primary,
        )
        self.assertEqual(recovered, public)

    def test_failed_public_check_preserves_candidate_and_publishes_reject(self) -> None:
        raw_task = valid_task()
        raw_task["repo"] = str(self.repo)
        raw_task["trials"] = 4
        raw_task["public_checks"] = [["python3", "-c", "raise SystemExit(1)"]]
        failing_task = TaskConfig.from_mapping(raw_task)
        frozen, request = freeze_candidate(
            self.experiment_directory,
            self.research_worktree,
            failing_task,
            self.manifest,
        )
        self.assertFalse(
            run_public_checks(
                self.task_directory,
                self.experiment_directory,
                failing_task,
                command_builder=self.unconfined,
            )
        )
        public = publish_candidate_rejection(
            failing_task,
            self.experiment_directory,
            request,
        )
        self.assertEqual(public["decision"], "REJECT")
        self.assertEqual(public["champion_after"], self.champion)
        self.assertEqual(
            resolve_commit(self.repo, "refs/arctl/demo/candidates/000001"),
            frozen.candidate,
        )

    def test_public_check_that_modifies_candidate_is_rejected(self) -> None:
        raw_task = valid_task()
        raw_task["repo"] = str(self.repo)
        raw_task["trials"] = 4
        raw_task["public_checks"] = [
            ["python3", "-c", "from pathlib import Path; Path('src/model.py').write_text('bad')"]
        ]
        mutating_task = TaskConfig.from_mapping(raw_task)
        self.freeze()
        self.assertFalse(
            run_public_checks(
                self.task_directory,
                self.experiment_directory,
                mutating_task,
                command_builder=self.unconfined,
            )
        )
        self.assertEqual(
            load_experiment(self.experiment_directory).public_checks_passed,
            False,
        )

    def test_public_checks_use_subject_sandbox_by_default(self) -> None:
        self.freeze()
        completed = {
            "schema_version": 1,
            "return_code": 0,
            "duration_seconds": 0.1,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
        }
        with (
            mock.patch(
                "arctl.experiment.sandbox_command",
                return_value=("codex", "sandbox", "--", "check"),
            ) as build,
            mock.patch(
                "arctl.experiment.sanitized_environment",
                return_value={"PATH": "/bin"},
            ) as environment,
            mock.patch(
                "arctl.experiment.run_or_load_once",
                return_value=completed,
            ) as run,
        ):
            self.assertTrue(
                run_public_checks(
                    self.task_directory,
                    self.experiment_directory,
                    self.task,
                )
            )
        worktree = self.task_directory / "worktrees" / "000001-candidate"
        output = self.experiment_directory / "outputs" / "public-check-0001"
        build.assert_called_once()
        built = build.call_args
        self.assertEqual(
            tuple(built.args[0][-len(self.task.public_checks[0]) :]),
            self.task.public_checks[0],
        )
        self.assertEqual(built.kwargs["cwd"], worktree)
        self.assertEqual(
            built.kwargs["read_paths"],
            (
                worktree,
                *command_runtime_read_paths(self.task.public_checks[0]),
            ),
        )
        self.assertEqual(built.kwargs["write_paths"], (output,))
        self.assertEqual(built.kwargs["profile"], "arctl-subject")
        environment.assert_called_once()
        self.assertEqual(run.call_args.args[1], ("codex", "sandbox", "--", "check"))
        self.assertEqual(run.call_args.kwargs["env"], {"PATH": "/bin"})

    def test_public_check_sandbox_launch_failure_is_a_system_error(self) -> None:
        self.freeze()
        failed = {
            "schema_version": 1,
            "return_code": 1,
            "stdout_bytes": 0,
            "stderr_bytes": 1,
        }
        with (
            mock.patch(
                "arctl.experiment.sandbox_command",
                return_value=("codex", "sandbox", "--", "check"),
            ),
            mock.patch(
                "arctl.experiment.sanitized_environment",
                return_value={"PATH": "/bin"},
            ),
            mock.patch(
                "arctl.experiment.run_or_load_once",
                return_value=failed,
            ),
        ):
            with self.assertRaisesRegex(StateError, "sandbox did not start"):
                run_public_checks(
                    self.task_directory,
                    self.experiment_directory,
                    self.task,
                )
        self.assertTrue(
            (self.experiment_directory / "public-check.failure.json").is_file()
        )

    def test_primary_trigger_requires_exactly_one_suspect_transition(self) -> None:
        _, _ = self.freeze()
        self.assertTrue(
            run_public_checks(
                self.task_directory,
                self.experiment_directory,
                self.task,
                command_builder=self.unconfined,
            )
        )
        mark_comparison_reserved(self.experiment_directory, kind="primary")
        primary = self.evidence(
            suspect_required=True,
            suspect_reason="timeout_shift",
        )
        provisional = save_comparison_result(self.experiment_directory, primary)
        self.assertEqual(provisional.state, "PROVISIONAL")
        self.assertEqual(provisional.decision, Decision.PROVISIONAL)
        mark_comparison_reserved(self.experiment_directory, kind="suspect")
        finalizing = save_comparison_result(
            self.experiment_directory,
            primary,
            self.evidence(kind="suspect", lower=-0.01),
        )
        self.assertEqual(finalizing.state, "FINALIZING")
        self.assertEqual(finalizing.decision, Decision.ARCHIVE)

    def test_comparison_failure_classification_preserves_valid_primary(self) -> None:
        _, request = self.freeze()
        self.assertTrue(
            run_public_checks(
                self.task_directory,
                self.experiment_directory,
                self.task,
                command_builder=self.unconfined,
            )
        )
        mark_comparison_reserved(self.experiment_directory, kind="primary")
        primary = self.evidence(
            suspect_required=True,
            suspect_reason="timeout_shift",
        )
        save_comparison_result(self.experiment_directory, primary)
        mark_comparison_reserved(self.experiment_directory, kind="suspect")
        public = publish_comparison_failure(
            self.task,
            self.experiment_directory,
            request,
            self.manifest,
            source="evaluator",
            primary=primary,
        )
        self.assertEqual(public["decision"], "INVALID")
        self.assertEqual(len(public["evaluation"]["comparisons"]), 1)
        self.assertEqual(public["failure"], "system_execution")
        self.assertEqual(
            public["failure_detail"],
            "Evaluator execution failed; no valid score was produced.",
        )

    def test_candidate_timeout_publishes_a_safe_explanation(self) -> None:
        _, request = self.freeze()
        self.assertTrue(
            run_public_checks(
                self.task_directory,
                self.experiment_directory,
                self.task,
                command_builder=self.unconfined,
            )
        )
        mark_comparison_reserved(self.experiment_directory, kind="primary")

        public = publish_comparison_failure(
            self.task,
            self.experiment_directory,
            request,
            self.manifest,
            source="candidate",
            cause="process timed out: /private/path/must-not-leak",
        )

        self.assertEqual(public["decision"], "REJECT")
        self.assertEqual(public["evaluation"]["comparisons"], [])
        self.assertEqual(
            public["failure_detail"],
            "Candidate exceeded the approved 60-second execution limit.",
        )
        self.assertNotIn("private", json.dumps(public))
        evaluation = (
            self.task_directory
            / "reports"
            / "experiments"
            / "000001"
            / "evaluation.md"
        ).read_text(encoding="utf-8")
        self.assertIn("## Execution failure", evaluation)
        self.assertIn(public["failure_detail"], evaluation)
