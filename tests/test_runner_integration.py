from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from arctl.approval import confirm_approval, preview_approval
from arctl.errors import StateError, ValidationError
from arctl.experiment import start_experiment
from arctl.git import create_candidate_commit, create_candidate_ref, resolve_commit
from arctl.models import CandidateReviewConfig, TaskConfig
from arctl.operations import inspect_experiment, task_report, task_status
from arctl.registry import LocatedTask
from arctl.runner import (
    _compatibility_strategy_command,
    _default_research_command,
    _run_compute_probe,
    _run_implementation,
    _run_research,
    _validate_implementation_report,
    run_task,
)
from arctl.search import search_ledger
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
        (self.subject / "ENVIRONMENT.md").write_text("Public environment rules.\n")
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

    def test_compute_probe_warns_without_gating_the_candidate(self) -> None:
        manifest, _ = load_manifest(self.task_directory / "evaluator.manifest.json")
        task = LocatedTask(
            self.task_directory,
            replace(self.config, public_probe_trial_equivalents=1),
        )
        attempt = self.task_directory / "searches" / "000001" / "attempts" / "01"
        with mock.patch(
            "arctl.runner.run_or_load_once",
            return_value={
                "schema_version": 2,
                "return_code": 0,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "elapsed_seconds": manifest.limits.timeout_seconds,
            },
        ):
            report = _run_compute_probe(
                task,
                manifest,
                attempt=attempt,
                worktree=self.subject,
                trial_count=4,
                command_builder=lambda command, cwd, output: command,
                stop_path=self.task_directory / "stop.requested",
            )

        self.assertEqual(report["risk"], "likely_over_budget")
        self.assertTrue(report["advisory_only"])
        self.assertEqual(
            json.loads((attempt / "compute-probe.public.json").read_text()),
            report,
        )

    @staticmethod
    def unconfined_public(command, _cwd, _output):
        return command

    @staticmethod
    def unconfined_comparison(command, _cwd, _reads, _writes, _profile):
        return command

    def test_status_attributes_the_approved_baseline(self) -> None:
        status = task_status(self.task)

        self.assertEqual(status["champion"], self.original_champion)
        self.assertEqual(
            status["champion_provenance"],
            {"kind": "initial", "experiment_id": None, "hypothesis": None},
        )

    def test_unlimited_task_honors_per_invocation_limit(self) -> None:
        directory = self.root / "data" / "tasks" / "unlimited"
        directory.mkdir()
        task_file = directory / "task.yaml"
        task_file.write_text("approved unlimited task\n")
        raw = valid_task()
        raw.update(
            {
                "task_id": "unlimited",
                "repo": str(self.subject),
                "editable_paths": ["subject.py"],
                "denied_paths": [".git/**"],
                "public_checks": [list(command) for command in self.config.public_checks],
                "evaluator": {
                    "repo": str(self.evaluator),
                    "commit": self.config.evaluator.commit,
                },
                "trials": 4,
                "max_experiments": "unlimited",
            }
        )
        config = TaskConfig.from_mapping(raw)
        preview = preview_approval(task_file, config)
        confirm_approval(directory, config, preview, preview.confirmation_token)
        task = LocatedTask(directory, config)

        outcome = run_task(
            task,
            max_experiments=1,
            research_command_builder=self.research_command,
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )

        self.assertEqual(len(outcome.results), 1)
        self.assertFalse(outcome.limit_reached)
        self.assertIsNone(task_status(task)["max_experiments"])
        self.assertEqual(task_status(task)["state"], "READY")

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
    "schema_version": 2,
    "strategy_behavior_id": "environment-compatible",
    "claim": "Add one point to each valid result.",
    "mechanism": "Increase the subject score by one.",
    "viability": "The score expression is directly adjustable.",
    "evidence_review": {
        "summary": "No prior experiment bears on the first candidate.",
        "citations": [],
    },
    "expected_effect": "The paired score difference is positive.",
    "expected_telemetry": {},
    "falsifiers": ["The paired effect is not positive."],
    "lineage": {
        "kind": "new",
        "prior_entry_id": None,
    },
}))
"""
        return ("python3", "-c", script, str(worktree), str(scratch))

    @staticmethod
    def planning_command(worktree: Path, scratch: Path, _schema: Path, _prompt: str):
        assert "selection must contain only the chosen strategy behavior id" in _prompt
        assert "never prescribe trial counts" in _prompt
        script = """\
import json
import sys
from pathlib import Path

worktree, scratch = map(Path, sys.argv[1:])
assert "+ 0" in (worktree / "subject.py").read_text()
request = {
    "schema_version": 2,
    "strategy_behavior_id": "environment-compatible",
    "claim": "Add one point to each valid result.",
    "mechanism": "Increase the subject score by one.",
    "viability": "The score expression is directly adjustable.",
    "evidence_review": {"summary": "No prior evidence.", "citations": []},
    "expected_effect": "The paired score difference is positive.",
    "expected_telemetry": {},
    "falsifiers": ["The paired effect is not positive."],
    "lineage": {"kind": "new", "prior_entry_id": None},
}
(scratch / "planning.public.json").write_text(json.dumps({
    "schema_version": 2,
    "directions": [{
        "strategy_behavior_id": "environment-compatible",
        "champion_assessment": "The baseline is environment compatible.",
        "remaining_gap": "Its score remains improvable.",
        "disposition": "candidate",
        "request": request,
        "evidence": ["The score expression contains a zero offset."],
        "feasibility": "One editable expression implements it.",
        "expected_value": "A positive paired difference.",
    }],
    "selection_rationale": "This is the only current direction.",
    "selection": request["strategy_behavior_id"],
}))
"""
        return ("python3", "-c", script, str(worktree), str(scratch))

    @staticmethod
    def implementation_command(
        worktree: Path, scratch: Path, _schema: Path, _prompt: str
    ):
        script = """\
import json
import sys
from pathlib import Path

worktree, scratch = map(Path, sys.argv[1:])
subject = worktree / "subject.py"
subject.write_text(subject.read_text().replace("+ 0", "+ 1"))
(scratch / "implementation.public.json").write_text(json.dumps({
    "schema_version": 1,
    "status": "implemented",
    "summary": "Implemented the frozen one-point mechanism.",
    "deviations": [],
}))
"""
        return ("python3", "-c", script, str(worktree), str(scratch))

    def test_planner_freezes_request_before_separate_implementation(self) -> None:
        outcome = run_task(
            self.task,
            max_experiments=1,
            planning_command_builder=self.planning_command,
            research_command_builder=self.implementation_command,
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )

        self.assertEqual(outcome.results[0]["decision"], "ACCEPT")
        attempt = self.task_directory / "searches" / "000001" / "attempts" / "01"
        plan = json.loads((attempt / "planning.public.json").read_text())
        request = json.loads((attempt / "request.public.json").read_text())
        self.assertEqual(plan["selection"], request["strategy_behavior_id"])
        self.assertEqual(plan["directions"][0]["request"], request)
        self.assertTrue((attempt / "implementation.public.json").is_file())

    def test_all_directions_exhausted_refreshes_strategy_then_replans(self) -> None:
        planning_calls = 0
        strategy_calls = 0

        def planning(worktree: Path, scratch: Path, schema: Path, prompt: str):
            nonlocal planning_calls
            planning_calls += 1
            if planning_calls > 1:
                return self.planning_command(worktree, scratch, schema, prompt)
            script = """\
import json, sys
from pathlib import Path
scratch = Path(sys.argv[1])
(scratch / "planning.public.json").write_text(json.dumps({
    "schema_version": 2,
    "directions": [{
        "strategy_behavior_id": "environment-compatible",
        "champion_assessment": "The champion already expresses this behavior.",
        "remaining_gap": "No credible gap remains under current evidence.",
        "disposition": "exhausted",
        "request": None,
        "evidence": ["Prior evidence leaves no material mechanism."],
        "feasibility": "No faithful material implementation is available.",
        "expected_value": "Further work would repeat prior evidence.",
    }],
    "selection_rationale": "All current directions are exhausted.",
    "selection": None,
}))
"""
            return ("python3", "-c", script, str(scratch))

        def strategy(worktree, scratch, schema, prompt):
            nonlocal strategy_calls
            strategy_calls += 1
            if strategy_calls > 1:
                script = """\
import json, sys
from pathlib import Path
scratch = Path(sys.argv[1])
(scratch / "strategy.public.json").write_text(json.dumps({
    "schema_version": 2,
    "environment_observations": [{
        "id": "environment-baseline",
        "claim": "The environment supports observable actions.",
        "status": "inferred",
        "evidence": [{
            "source_id": "public-environment",
            "location": "refreshed analysis",
            "finding": "Observable actions remain available.",
        }],
    }],
    "environment_uncertainties": [],
    "successful_policy_behaviors": [{
        "id": "environment-compatible",
        "behavior": "Exploit observable action consequences.",
        "derived_from": ["environment-baseline"],
        "rationale": "The refreshed analysis emphasizes consequences.",
        "tradeoffs": [],
    }],
}))
"""
                return ("python3", "-c", script, str(scratch))
            return _compatibility_strategy_command(worktree, scratch, schema, prompt)

        outcome = run_task(
            self.task,
            max_experiments=1,
            planning_command_builder=planning,
            research_command_builder=self.implementation_command,
            strategy_command_builder=strategy,
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )

        self.assertEqual(len(outcome.results), 1)
        self.assertEqual(planning_calls, 2)
        self.assertEqual(strategy_calls, 2)
        exhausted = search_ledger(
            self.task_directory,
            query="directions_exhausted",
            path=None,
            decision=None,
        )
        self.assertEqual(len(exhausted), 1)
        self.assertIsNone(exhausted[0]["planning"]["selection"])
        self.assertEqual(len(list((self.task_directory / "experiments").glob("*"))), 1)

    @staticmethod
    def passing_review_command(
        _worktree: Path, scratch: Path, _schema: Path, _prompt: str
    ):
        script = """\
import json, sys
from pathlib import Path
scratch = Path(sys.argv[1])
(scratch / 'review.public.json').write_text(json.dumps({
    'schema_version': 1,
    'verdict': 'pass',
    'summary': 'The candidate uses only the declared interface.',
    'findings': [],
}))
"""
        return ("python3", "-c", script, str(scratch))

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
        status = task_status(self.task)
        self.assertEqual(
            status["champion_provenance"],
            {
                "kind": "experiment",
                "experiment_id": 1,
                "hypothesis": outcome.results[0]["hypothesis"],
            },
        )
        inspected = inspect_experiment(self.task, 1)
        self.assertEqual(
            inspected["champion_after_provenance"],
            status["champion_provenance"],
        )
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
        self.assertIn("strategy_behavior_id", prompts[0])
        self.assertIn("prior results, telemetry, and reflections", prompts[0])
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
                "strategy",
                "search_attempt",
                "compute_probe",
                "experiment",
                "candidate",
                "public_checks",
                "public_checks_complete",
                "comparison",
                "reflection",
                "reflection_complete",
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
        dossier = self.task_directory / "reports" / "experiments" / "000001"
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

    def test_reviewed_candidate_passes_before_experiment_and_is_reported(self) -> None:
        config = replace(
            self.config,
            candidate_review=CandidateReviewConfig(
                contract="Use only supplied observations.",
                checks=(("python3", "-c", "raise SystemExit(0)"),),
                repair_attempts=1,
            ),
        )
        task = LocatedTask(self.task_directory, config)
        review_prompts: list[str] = []
        events: list[dict] = []

        def review(_worktree: Path, scratch: Path, _schema: Path, prompt: str):
            review_prompts.append(prompt)
            script = """\
import json, sys
from pathlib import Path
scratch = Path(sys.argv[1])
(scratch / 'review.public.json').write_text(json.dumps({
    'schema_version': 1,
    'verdict': 'pass',
    'summary': 'The candidate uses only the declared interface.',
    'findings': [],
}))
"""
            return ("python3", "-c", script, str(scratch))

        outcome = run_task(
            task,
            max_experiments=1,
            research_command_builder=self.research_command,
            review_command_builder=review,
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
            progress=events.append,
        )

        self.assertEqual(len(outcome.results), 1)
        self.assertEqual(len(review_prompts), 1)
        self.assertNotIn(str(self.evaluator), review_prompts[0])
        experiment = self.task_directory / "experiments" / "000001"
        self.assertTrue(
            (
                experiment
                / "candidate-review"
                / "round-01"
                / "decision.public.json"
            ).is_file()
        )
        dossier = Path(task_report(task)["results"][0]["dossier_path"])
        self.assertTrue((dossier.parent / "candidate-review.md").is_file())
        kinds = [event["event"] for event in events]
        self.assertLess(kinds.index("candidate_review"), kinds.index("experiment"))

    def test_reviewer_infrastructure_failure_resumes_same_candidate(self) -> None:
        config = replace(
            self.config,
            candidate_review=CandidateReviewConfig(
                contract="Use only supplied observations.",
                checks=(("python3", "-c", "raise SystemExit(0)"),),
                repair_attempts=1,
            ),
        )
        task = LocatedTask(self.task_directory, config)

        def failed_review(_worktree: Path, _scratch: Path, _schema: Path, _prompt: str):
            return ("python3", "-c", "raise SystemExit(1)")

        with self.assertRaisesRegex(StateError, "review session exited"):
            run_task(
                task,
                max_experiments=1,
                research_command_builder=self.research_command,
                review_command_builder=failed_review,
                public_check_command_builder=self.unconfined_public,
                comparison_command_builder=self.unconfined_comparison,
            )
        self.assertFalse((self.task_directory / "experiments").exists())

        outcome = run_task(
            task,
            max_experiments=1,
            research_command_builder=lambda *_: self.fail(
                "research reran instead of resuming candidate review"
            ),
            review_command_builder=self.passing_review_command,
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )

        self.assertEqual(len(outcome.results), 1)
        attempts = (
            self.task_directory
            / "searches"
            / "000001"
            / "attempts"
            / "01"
            / "candidate-review"
            / "round-01"
            / "semantic"
            / "attempts"
        )
        self.assertEqual([path.name for path in sorted(attempts.iterdir())], ["0001", "0002"])

    def test_later_executor_starts_from_latest_accepted_champion(self) -> None:
        seen: list[str] = []

        def research(worktree: Path, scratch: Path, schema: Path, prompt: str):
            current = (worktree / "subject.py").read_text()
            seen.append(current)
            command = list(self.research_command(worktree, scratch, schema, prompt))
            if "+ 1" in current:
                command[2] = command[2].replace(
                    'replace("+ 0", "+ 1")',
                    'replace("+ 1", "+ 2")',
                )
            return tuple(command)

        task = LocatedTask(
            self.task_directory,
            replace(self.config, max_experiments=2),
        )
        outcome = run_task(
            task,
            max_experiments=2,
            research_command_builder=research,
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )

        self.assertEqual(
            [result["decision"] for result in outcome.results],
            ["ACCEPT", "ACCEPT"],
        )
        self.assertIn("+ 0", seen[0])
        self.assertIn("+ 1", seen[1])
        self.assertNotIn("+ 0", seen[1])

    def test_reflection_failure_blocks_search_and_next_run_recovers_it(self) -> None:
        def fail_reflection(**arguments):
            attempt = arguments["experiment"] / "reflection" / "attempts" / "0001"
            attempt.mkdir(parents=True)
            (attempt / "reflection.failure.json").write_text(
                json.dumps({"schema_version": 1, "message": "backend failed"})
            )
            raise StateError("reflection backend failed")

        with mock.patch(
            "arctl.reflection.run_reflection",
            side_effect=fail_reflection,
        ):
            failed = run_task(
                self.task,
                max_experiments=1,
                research_command_builder=self.research_command,
                public_check_command_builder=self.unconfined_public,
                comparison_command_builder=self.unconfined_comparison,
            )

        self.assertTrue(failed.reflection_failed)
        self.assertEqual(failed.reflection_error, "reflection backend failed")
        self.assertEqual(failed.results[0]["decision"], "ACCEPT")
        self.assertEqual(task_status(self.task)["state"], "REFLECTION_FAILED")
        experiment = self.task_directory / "experiments" / "000001"
        self.assertFalse((experiment / "published").exists())

        recovered = run_task(
            self.task,
            max_experiments=1,
            research_command_builder=lambda *_: self.fail(
                "candidate search ran before reflection recovery"
            ),
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )
        self.assertFalse(recovered.reflection_failed)
        self.assertTrue((experiment / "published").is_file())
        self.assertEqual(task_status(self.task)["state"], "LIMIT_REACHED")

    def test_exact_duplicates_refresh_strategy_then_stall_without_an_experiment(self) -> None:
        (self.subject / "subject.py").write_text(
            (self.subject / "subject.py").read_text().replace("+ 0", "+ 1")
        )
        prior, _ = create_candidate_commit(
            self.subject,
            champion=self.original_champion,
            editable_paths=("subject.py",),
            denied_paths=(".git/**",),
            prior_candidate_ref_prefix="refs/arctl/demo/candidates/",
            message="prior candidate",
        )
        create_candidate_ref(
            self.subject,
            "refs/arctl/demo/candidates/000099",
            prior,
        )
        git(self.subject, "restore", "--staged", "--worktree", "subject.py")
        strategy_calls: list[str] = []

        def strategy(worktree, scratch, schema, prompt):
            strategy_calls.append(prompt)
            return _compatibility_strategy_command(worktree, scratch, schema, prompt)

        outcome = run_task(
            self.task,
            max_experiments=1,
            research_command_builder=self.research_command,
            strategy_command_builder=strategy,
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )

        self.assertTrue(outcome.stalled)
        self.assertEqual(outcome.results, ())
        self.assertEqual(len(strategy_calls), 2)
        for prompt in strategy_calls:
            self.assertIn("environment_sources", prompt)
            self.assertIn("evaluation_boundary", prompt)
            self.assertNotIn("exploration_ledger", prompt)
            self.assertNotIn('"statistic"', prompt)
            self.assertNotIn('"telemetry"', prompt)
        self.assertFalse((self.task_directory / "experiments").exists())
        misses = search_ledger(
            self.task_directory,
            decision=None,
            path=None,
            query="exact_duplicate",
        )
        self.assertEqual(len(misses), 6)
        self.assertEqual(task_status(self.task)["state"], "READY")

    def test_malformed_requests_are_bounded_search_misses(self) -> None:
        def malformed(_worktree, scratch, _schema, _prompt):
            script = "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('{}')"
            return ("python3", "-c", script, str(scratch / "request.public.json"))

        outcome = run_task(
            self.task,
            max_experiments=1,
            research_command_builder=malformed,
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )

        self.assertTrue(outcome.stalled)
        misses = search_ledger(
            self.task_directory,
            query="invalid_request",
            path=None,
            decision=None,
        )
        self.assertEqual(len(misses), 6)

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
                        "schema_version": 2,
                        "strategy_behavior_id": "environment-compatible",
                        "claim": "Use the approved public runtime.",
                        "mechanism": "Run public development tools.",
                        "viability": "The approved runtime is readable.",
                        "evidence_review": {
                            "summary": "No relevant prior evidence.",
                            "citations": [],
                        },
                        "expected_effect": "Improve the score.",
                        "expected_telemetry": {},
                        "falsifiers": ["The effect is not positive."],
                        "lineage": {"kind": "new", "prior_entry_id": None},
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
        self.assertEqual(captured["model"], "gpt-5.6-terra")
        self.assertEqual(captured["reasoning_effort"], "medium")

    def test_default_implementation_receives_public_runtime_paths(self) -> None:
        attempt = self.task_directory / "searches" / "000001" / "attempts" / "01"
        attempt.mkdir(parents=True)
        manifest = load_manifest(self.task_directory / "evaluator.manifest.json")[0]
        runtime = self.root / "approved-runtime"
        runtime.mkdir()
        captured: dict = {}

        def build_implementation_command(agent, request):
            captured.update(
                {
                    "model": agent.model,
                    "read_paths": request.read_paths,
                    "scratch": request.scratch,
                    "prompt": request.prompt,
                    "schema": json.loads(request.output_schema.read_text()),
                }
            )
            script = (
                "import json,pathlib,sys;"
                "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
                "'schema_version':2,'status':'implemented','summary':'done',"
                "'deviations':[],'requirements':[{'requirement':'use runtime',"
                "'status':'verified','evidence':'policy.py:1'}]}))"
            )
            return (
                "python3",
                "-c",
                script,
                str(request.scratch / "implementation.public.json"),
            )

        with (
            mock.patch(
                "arctl.runner.command_runtime_read_paths",
                return_value=(runtime,),
            ),
            mock.patch(
                "arctl.runner.agent_command",
                side_effect=build_implementation_command,
            ),
        ):
            _run_implementation(
                self.task,
                manifest,
                attempt,
                self.subject,
                {"claim": "test the runtime"},
                trial_count=4,
                command_builder=None,
                stop_path=self.task_directory / "stop.requested",
            )

        self.assertEqual(captured["read_paths"], (runtime,))
        self.assertEqual(captured["model"], "gpt-5.6-terra")
        self.assertEqual(captured["schema"]["properties"]["schema_version"]["const"], 2)
        self.assertIn("sentence by sentence", captured["prompt"])
        self.assertIn(manifest.subject_interface, captured["prompt"])

    def test_implemented_report_requires_every_requirement_verified(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unverified requirements"):
            _validate_implementation_report(
                {
                    "schema_version": 2,
                    "status": "implemented",
                    "summary": "Claimed complete.",
                    "deviations": [],
                    "requirements": [
                        {
                            "requirement": "Validate transitions.",
                            "status": "unverified",
                            "evidence": "No targeted probe exists.",
                        }
                    ],
                }
            )

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

        promoted = resolve_commit(self.subject, "refs/arctl/auto/champion")
        stale_calibration_worktree = (
            auto_directory / "worktrees" / "calibration-champion"
        )
        git(
            self.subject,
            "worktree",
            "add",
            "--force",
            "--detach",
            str(stale_calibration_worktree),
            promoted,
        )
        resumed = run_task(
            task,
            max_experiments=1,
            research_command_builder=lambda *_: (_ for _ in ()).throw(
                AssertionError("completed task started another research session")
            ),
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
            calibration_command_builder=lambda *_: (_ for _ in ()).throw(
                AssertionError("completed calibration was rerun after promotion")
            ),
        )
        self.assertEqual(resumed.results, ())

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
        self.assertFalse((self.task_directory / "experiments").exists())
        self.assertFalse(
            (self.task_directory / "worktrees" / "search-000001-attempt-01").exists()
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
        attempt = self.task_directory / "searches" / "000001" / "attempts" / "01"
        self.assertTrue((attempt / "research.failure.json").is_file())
        self.assertTrue((attempt / "process" / "research" / "result.json").is_file())
        self.assertEqual(task_status(self.task)["state"], "RESEARCH_FAILED")

        outcome = run_task(
            self.task,
            max_experiments=1,
            research_command_builder=self.research_command,
            public_check_command_builder=self.unconfined_public,
            comparison_command_builder=self.unconfined_comparison,
        )
        self.assertEqual(outcome.results[0]["experiment_id"], 1)
        self.assertTrue((attempt / "research.failure.json").exists())

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
