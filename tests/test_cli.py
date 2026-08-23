from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from arctl.cli import (
    _ProgressView,
    _SetupProgressView,
    _emit,
    _invoked_program,
    _progress,
    _rewrite_next_command,
    _run,
    _setup_proposal_table,
    _status,
    build_parser,
    main,
    render_cli_reference,
)
from arctl.errors import TransientDownstreamError
from arctl.runner import RunOutcome
from arctl.experiment import start_experiment


class CliTests(unittest.TestCase):
    @staticmethod
    def initialize_repo(repo: Path) -> None:
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        (repo / "ENVIRONMENT.md").write_text("# Environment\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "ENVIRONMENT.md"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=arctl tests",
                "-c",
                "user.email=tests@invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            check=True,
        )

    def run_cli(self, arguments: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(arguments)
        return code, output.getvalue()

    def test_init_creates_one_editable_task_and_json_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "subject"
            repo.mkdir()
            self.initialize_repo(repo)
            code, output = self.run_cli(
                [
                    "--data",
                    str(root / "data"),
                    "init",
                    "--repo",
                    str(repo),
                    "--json",
                ]
            )
            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["state"], "TASK_DRAFT")
            self.assertTrue(payload["action_required"])
            self.assertEqual(
                payload["next_command"],
                f"arctl --data {root / 'data'} approve subject",
            )
            task = root / "data" / "tasks" / "subject" / "task.yaml"
            task_text = task.read_text()
            self.assertIn(f'repo: "{repo}"', task_text)
            self.assertIn("schema_version: 4", task_text)
            self.assertIn("codebases:", task_text)
            self.assertIn("profile: serial-v1", task_text)
            self.assertIn("allow_unverified_isolation: false", task_text)
            self.assertIn("max_experiments: 1000", task_text)

            code, output = self.run_cli(
                [
                    "--data",
                    str(root / "data"),
                    "history",
                    "subject",
                    "--query",
                    "routing",
                    "--json",
                ]
            )
            self.assertEqual(code, 0)
            history = json.loads(output)
            self.assertEqual(history["state"], "HISTORY")
            self.assertEqual(history["history"]["entries"], [])

            code, output = self.run_cli(
                [
                    "--data",
                    str(root / "data"),
                    "init",
                    "--repo",
                    str(repo),
                    "--json",
                ]
            )
            self.assertEqual(code, 1)
            error = json.loads(output)
            self.assertFalse(error["success"])
            self.assertIn("Failed:", error["message"])
            self.assertTrue(error["evidence_valid"])
            self.assertFalse(error["can_continue"])
            self.assertIn("log_path", error)

    def test_guided_init_creates_visible_workspace_without_task_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "subject"
            repo.mkdir()
            self.initialize_repo(repo)
            workspace = root / "subject-research"
            data = workspace / ".arctl-data"

            code, output = self.run_cli(
                [
                    "--data",
                    str(data),
                    "init",
                    "--repo",
                    str(repo),
                    "--workspace",
                    str(workspace),
                    "--json",
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(payload["state"], "SETUP_DISCOVERY_REQUIRED")
            self.assertTrue((workspace / "arctl.workspace.yaml").is_file())
            self.assertTrue((data / "tasks" / "subject" / "setup.json").is_file())
            configured = subprocess.run(
                ["git", "-C", str(repo), "config", "--local", "--get", "arctl.dataRoot"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(configured, str(data))
            status = _status(data, "subject")
            self.assertEqual(status["state"], "SETUP_STATUS")
            self.assertEqual(status["setup_state"], "DISCOVERY_REQUIRED")
            self.assertEqual(status["allowed_actions"], ["setup"])

    def test_human_output_omits_machine_next_command(self) -> None:
        code, output = self.run_cli(["doctor"])
        self.assertIn(code, (0, 1))
        next_lines = [line for line in output.splitlines() if line.startswith("Next: ")]
        self.assertEqual(next_lines, [])

    def test_root_help_is_exhaustive_and_cli_reference_is_in_sync(self) -> None:
        help_text = build_parser().format_help()
        for command in (
            "doctor",
            "init",
            "approve",
            "run",
            "status",
            "stop",
            "report",
            "history",
            "inspect",
        ):
            self.assertIn(command, help_text)
        self.assertIn("Typical workflow:", help_text)
        self.assertIn("approval-locked evaluator", help_text)
        self.assertIn("AI-orchestration", help_text)
        reference = Path("docs/cli-reference.md").read_text(encoding="utf-8")
        self.assertEqual(reference, render_cli_reference())

    def test_human_run_output_reports_exhaustion_instead_of_zero_work(self) -> None:
        payload = {
            "schema_version": 1,
            "success": True,
            "task_id": "demo",
            "experiment_id": None,
            "state": "LIMIT_REACHED",
            "action_required": False,
            "allowed_actions": ["status", "report"],
            "artifacts": [],
            "message": "Task demo reached its approved experiment limit.",
            "evidence_valid": None,
            "can_continue": True,
            "log_path": None,
            "next_command": "arctl report demo",
            "results": [],
            "experiment_limit": {"completed": 10, "maximum": 10},
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _emit(payload, as_json=False)

        self.assertEqual(output.getvalue(), "Experiment limit reached: 10/10.\n")
        self.assertNotIn("Done: 0 tested", output.getvalue())

    def test_human_status_output_reports_exhaustion(self) -> None:
        payload = {
            "schema_version": 1,
            "success": True,
            "task_id": "demo",
            "experiment_id": 10,
            "state": "LIMIT_REACHED",
            "action_required": False,
            "allowed_actions": ["status", "report", "history"],
            "artifacts": [],
            "message": "Limit reached.",
            "evidence_valid": None,
            "can_continue": True,
            "log_path": None,
            "next_command": "arctl report demo",
            "status": {
                "state": "LIMIT_REACHED",
                "trial_count": 64,
                "champion": "a" * 40,
                "last_result": None,
                "provisional": False,
                "stop_requested": False,
                "strategy_revision": 1,
                "search_id": 2,
                "search_attempt": 1,
                "completed_experiments": 10,
                "max_experiments": 10,
                "calibration_summary": None,
            },
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _emit(payload, as_json=False)

        self.assertIn("│ State            │ LIMIT_REACHED", output.getvalue())
        self.assertIn("│ Experiment limit │ 10/10 · reached", output.getvalue())

    def test_status_table_names_promoting_experiment_and_stays_compact(self) -> None:
        payload = {
            "schema_version": 1,
            "success": True,
            "task_id": "demo",
            "state": "READY",
            "message": "Ready.",
            "log_path": "/tmp/demo",
            "status": {
                "state": "READY",
                "trial_count": 64,
                "champion": "b" * 40,
                "champion_provenance": {
                    "kind": "experiment",
                    "experiment_id": 2,
                    "hypothesis": "Use a structural lookahead policy.\nIgnore noise.",
                },
                "last_result": None,
                "provisional": False,
                "stop_requested": False,
            },
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _emit(payload, as_json=False)

        rendered = output.getvalue()
        self.assertIn("bbbbbbbbbbbb (Expt #2)", rendered)
        self.assertIn("Use a structural lookahead policy. Ignore noise.", rendered)
        self.assertLessEqual(max(map(len, rendered.splitlines())), 140)

    def test_status_table_explains_automatic_trial_count(self) -> None:
        payload = {
            "schema_version": 1,
            "success": True,
            "task_id": "demo",
            "state": "READY",
            "message": "Ready.",
            "log_path": "/tmp/demo",
            "status": {
                "state": "READY",
                "calibration": "complete",
                "trial_count": 64,
                "champion": None,
                "last_result": None,
                "provisional": False,
                "stop_requested": False,
            },
        }
        calibrated = io.StringIO()
        with contextlib.redirect_stdout(calibrated):
            _emit(payload, as_json=False)
        self.assertIn("64 paired trials (autocalibrated)", calibrated.getvalue())

        payload["state"] = "CALIBRATION_REQUIRED"
        payload["status"]["state"] = "CALIBRATION_REQUIRED"
        payload["status"]["calibration"] = "not_started"
        payload["status"]["trial_count"] = None
        pending = io.StringIO()
        with contextlib.redirect_stdout(pending):
            _emit(payload, as_json=False)
        self.assertIn("Unfrozen (to be autocalibrated)", pending.getvalue())

        payload["status"]["max_experiments"] = None
        unlimited = io.StringIO()
        with contextlib.redirect_stdout(unlimited):
            _emit(payload, as_json=False)
        self.assertIn("│ Experiment limit │ Unlimited", unlimited.getvalue())

    def test_report_table_prints_dossier_root_once_and_preserves_exact_json(self) -> None:
        root = "/tmp/" + "long-root/" * 10 + "reports/experiments"
        result = {
            "experiment_id": 12,
            "decision": "ARCHIVE",
            "hypothesis": "A broad algorithmic change " + "improves play " * 12,
            "evaluation": {
                "comparisons": [
                    {
                        "effect_estimate": 0.03123456789,
                        "one_sided_lower_bound": -6.391185814799755,
                    }
                ]
            },
            "dossier_path": root + "/000012/README.md",
        }
        payload = {
            "schema_version": 1,
            "success": True,
            "task_id": "demo",
            "state": "REPORT",
            "message": "Report.",
            "report": {
                "completed_experiments": 1,
                "results": [result],
                "dossier_root": root,
                "calibration_summary": None,
            },
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _emit(payload, as_json=False)

        rendered = output.getvalue()
        self.assertEqual(rendered.count(root), 1)
        self.assertIn("0.031235", rendered)
        self.assertIn("LB -6.3912", rendered)
        self.assertIn("000012", rendered)
        self.assertLessEqual(max(map(len, rendered.splitlines())), 140)
        self.assertEqual(
            payload["report"]["results"][0]["evaluation"]["comparisons"][0][
                "effect_estimate"
            ],
            0.03123456789,
        )
        machine = io.StringIO()
        with contextlib.redirect_stdout(machine):
            _emit(payload, as_json=True)
        self.assertEqual(
            json.loads(machine.getvalue())["report"]["results"][0]["evaluation"][
                "comparisons"
            ][0]["effect_estimate"],
            0.03123456789,
        )

    def test_scoreless_failure_is_explained_in_human_views(self) -> None:
        result = {
            "experiment_id": 1,
            "decision": "REJECT",
            "hypothesis": "Use bounded beam search.",
            "candidate": "b" * 40,
            "champion_after": "a" * 40,
            "evaluation": {"comparisons": []},
            "failure": "candidate_execution",
            "failure_detail": "Candidate exceeded the approved 300-second execution limit.",
            "dossier_path": "/tmp/reports/experiments/000001/README.md",
        }
        payload = {
            "schema_version": 1,
            "success": True,
            "task_id": "demo",
            "state": "REPORT",
            "message": "Report.",
            "report": {
                "completed_experiments": 1,
                "results": [result],
                "dossier_root": "/tmp/reports/experiments",
                "calibration_summary": None,
            },
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _emit(payload, as_json=False)

        rendered = output.getvalue()
        self.assertIn("No score", rendered)
        self.assertIn("approved 300-second", rendered)
        self.assertIn("execution limit", rendered)
        self.assertLessEqual(max(map(len, rendered.splitlines())), 140)

    def test_run_retries_one_transient_failure_without_expanding_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_directory = root / "tasks" / "demo"
            task_directory.mkdir(parents=True)
            task = SimpleNamespace(
                directory=task_directory,
                config=SimpleNamespace(task_id="demo", max_experiments=1),
            )
            transient = TransientDownstreamError(
                "execution",
                "capacity",
                "Selected model is at capacity.",
                str(root / "process"),
            )
            result = {
                "experiment_id": 1,
                "decision": "REJECT",
                "hypothesis": "test",
            }
            with (
                mock.patch("arctl.cli._located", return_value=task),
                mock.patch(
                    "arctl.runner.run_task",
                    side_effect=(transient, RunOutcome((result,), False)),
                ) as run,
            ):
                payload = _run(
                    root,
                    "demo",
                    1,
                    retries=1,
                    retry_delay=0,
                    preflight=False,
                )

            self.assertTrue(payload["success"])
            self.assertEqual(payload["results"], (result,))
            self.assertEqual(run.call_count, 2)
            self.assertEqual(
                [call.kwargs["max_experiments"] for call in run.call_args_list],
                [1, 1],
            )

    def test_stop_during_search_does_not_claim_zero_experiments_after_prior_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_directory = root / "tasks" / "demo"
            prior = task_directory / "experiments" / "000004"
            prior.mkdir(parents=True)
            (prior / "published").touch()
            task = SimpleNamespace(
                directory=task_directory,
                config=SimpleNamespace(task_id="demo", max_experiments=10),
            )
            with (
                mock.patch("arctl.cli._located", return_value=task),
                mock.patch(
                    "arctl.runner.run_task",
                    return_value=RunOutcome((), True),
                ),
            ):
                payload = _run(root, "demo", 1, preflight=False)

            self.assertEqual(payload["state"], "STOPPED")
            self.assertEqual(
                payload["message"],
                "Task demo stopped safely during candidate search; "
                "no experiments completed in this run.",
            )

    def test_exhausted_run_reports_limit_without_starting_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_directory = root / "tasks" / "demo"
            experiment = task_directory / "experiments" / "000001"
            experiment.mkdir(parents=True)
            (experiment / "published").write_text("")
            task = SimpleNamespace(
                directory=task_directory,
                config=SimpleNamespace(task_id="demo", max_experiments=1),
            )
            with (
                mock.patch("arctl.cli._located", return_value=task),
                mock.patch("arctl.runner.run_task") as run,
            ):
                payload = _run(root, "demo", None, preflight=False)

            run.assert_not_called()
            self.assertEqual(payload["state"], "LIMIT_REACHED")
            self.assertEqual(
                payload["experiment_limit"], {"completed": 1, "maximum": 1}
            )
            self.assertEqual(payload["results"], ())

    def test_retryable_failure_exposes_safe_detail_and_artifact_path(self) -> None:
        failure = TransientDownstreamError(
            "execution",
            "capacity",
            "Selected model is at capacity.",
            "/tmp/research-process",
        )
        failure.retries_used = 2
        failure.max_retries = 2
        with mock.patch("arctl.cli._run", side_effect=failure):
            code, output = self.run_cli(["run", "demo", "--json"])

        payload = json.loads(output)
        self.assertEqual(code, 1)
        self.assertTrue(payload["can_continue"])
        self.assertEqual(payload["failure"]["category"], "capacity")
        self.assertEqual(payload["failure"]["retries_used"], 2)
        self.assertEqual(payload["log_path"], "/tmp/research-process")

    def test_approval_table_is_compact_ordered_and_actionable(self) -> None:
        payload = {
            "schema_version": 1,
            "success": True,
            "task_id": "demo",
            "experiment_id": None,
            "state": "APPROVAL_REQUIRED",
            "action_required": True,
            "allowed_actions": ["approve"],
            "artifacts": [],
            "message": "Approval required for task demo.",
            "evidence_valid": None,
            "can_continue": True,
            "log_path": None,
            "next_command": "arctl approve demo --confirm abc123",
            "approval": {
                "task_sha256": "a" * 64,
                "evaluator_commit": "b" * 40,
                "manifest_sha256": "c" * 64,
                "confirmation_token": "abc123",
            },
            "approval_summary": {
                "models": (
                    "gpt-5.6-sol high (Strategy + reflection); "
                    "gpt-5.6-terra medium (Execution)"
                ),
                "editable_paths": ["src/**", "tests/**"],
                "environment": "environment-core, public-probe",
                "trial_seeds": (
                    "Hidden seeds test both champion and candidate; not reused "
                    "within this task. Evaluator mapping: seed initializes cases."
                ),
                "trial_count": (
                    "Sweep [8, 16, 32, 64]; first meeting standard error ≤ 3; "
                    "otherwise 64."
                ),
                "success_criterion": (
                    "Hard rules pass; lower bound > 0 — candidate scores higher."
                ),
                "telemetry": {
                    "higher": [
                        {"name": "score", "description": "How well the policy plays."}
                    ],
                    "lower": [
                        {"name": "errors", "description": "How often actions fail."}
                    ],
                    "contextual": [
                        {"name": "diverged", "description": "Whether paths differ."}
                    ],
                },
                "variance_risks": "Random pieces. Mitigation: identical paired cases.",
            },
        }
        _rewrite_next_command(payload, "./.venv/bin/arctl")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _emit(payload, as_json=False)
        rendered = output.getvalue()
        labels = [
            "Models",
            "Environment",
            "Editable paths",
            "Trial seeds",
            "Trial count",
            "Success criterion",
            "Telemetry",
            "Variance risks",
            "Approval token",
            "Approval command",
        ]
        positions = [rendered.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Higher is better:", rendered)
        self.assertIn("Lower is better:", rendered)
        self.assertIn("Diagnostic:", rendered)
        self.assertIn("./.venv/bin/arctl approve demo --confirm abc123", rendered)
        self.assertNotIn("Denied paths", rendered)
        self.assertNotIn("Next:", rendered)
        self.assertNotIn("actions per case", rendered)
        self.assertEqual(rendered.count("Note:"), 1)
        self.assertLessEqual(max(map(len, rendered.splitlines())), 138)

    def test_human_success_omits_generic_machine_boilerplate(self) -> None:
        payload = {
            "schema_version": 1,
            "success": True,
            "task_id": "demo",
            "experiment_id": None,
            "state": "APPROVED",
            "action_required": False,
            "allowed_actions": ["run"],
            "artifacts": [],
            "message": "Approved and locked task demo.",
            "evidence_valid": None,
            "can_continue": True,
            "log_path": "/private/task",
            "next_command": "arctl run demo",
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _emit(payload, as_json=False)
        rendered = output.getvalue()
        self.assertNotIn("Saved evidence", rendered)
        self.assertNotIn("Work can continue", rendered)
        self.assertNotIn("User action required", rendered)
        self.assertEqual(rendered.splitlines()[-1], "Approved and locked task demo.")

    def test_progress_is_safe_single_line_narration(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _progress(
                {
                    "event": "candidate",
                    "candidate": "a" * 40,
                    "claim": "Try this.\n\u001b[31mFAKE STATUS",
                }
            )
        rendered = output.getvalue()
        self.assertNotIn("\u001b", rendered)
        self.assertNotIn("\nFAKE STATUS", rendered)
        self.assertIn("Candidate: aaaaaaaaaaaa", rendered)

    def test_non_tty_progress_shows_fsm_duration_without_ansi(self) -> None:
        now = [10.0]
        output = io.StringIO()
        view = _ProgressView(
            output,
            clock=lambda: now[0],
            interactive=False,
        )
        view({"event": "research"})
        now[0] = 12.5
        view(
            {
                "event": "candidate",
                "candidate": "b" * 40,
                "claim": "Focused change.",
            }
        )
        view.close()
        rendered = output.getvalue()
        self.assertIn("✓ RESEARCHING · 2.5s", rendered)
        self.assertIn("✓ CANDIDATE_FROZEN", rendered)
        self.assertNotIn("\033", rendered)

    def test_progress_explains_strategy_attempts_and_search_misses(self) -> None:
        output = io.StringIO()
        view = _ProgressView(output, interactive=False)
        view({"event": "strategy", "revision": 1, "refresh": False})
        view({"event": "search_attempt", "attempt": 1, "attempts": 6})
        view(
            {
                "event": "search_miss",
                "code": "policy_review_failed",
                "message": (
                    "transition fidelity — policy/greedy.py simulates on a board "
                    "that still contains the active piece " + "and cannot validate " * 8
                ),
            }
        )
        view.close()
        rendered = output.getvalue()
        self.assertIn("Strategy · revision 1", rendered)
        self.assertIn("candidate search · attempt 1/6", rendered)
        self.assertIn("Miss: transition fidelity", rendered)
        self.assertLessEqual(max(map(len, rendered.splitlines())), 140)

    def test_progress_surfaces_the_reflection_failure_reason(self) -> None:
        output = io.StringIO()
        view = _ProgressView(output, interactive=False)
        view({"event": "reflection"})
        view(
            {
                "event": "reflection_failed",
                "message": "reflection cites an unknown exploration entry",
            }
        )
        view.close()

        rendered = output.getvalue()
        self.assertIn("✗ REFLECTING", rendered)
        self.assertIn(
            "Reason: reflection cites an unknown exploration entry",
            rendered,
        )

    def test_each_progress_stage_requires_only_its_own_fields(self) -> None:
        events = [
            {"scope": "calibration", "stage": "reserve"},
            {"scope": "calibration", "stage": "prepare"},
            {"scope": "calibration", "stage": "champion_pilot"},
            {"scope": "calibration", "stage": "assessment"},
            {"scope": "calibration", "stage": "freeze"},
            {"scope": "comparison", "stage": "comparison"},
            {"scope": "comparison", "stage": "prepare"},
            {
                "scope": "comparison",
                "stage": "subject",
                "batch": 1,
                "batches": 2,
            },
            {"scope": "comparison", "stage": "score"},
            {"scope": "comparison", "stage": "validate"},
        ]
        labels = [_ProgressView._stage_label(event) for event in events]
        self.assertEqual(labels[6], "evaluator prepare")
        self.assertEqual(labels[7], "subject batch 1/2")

    def test_setup_progress_is_visible_and_timed(self) -> None:
        output = io.StringIO()
        view = _SetupProgressView(output, interactive=False)
        view({"stage": "repository discovery", "status": "started"})
        view({"stage": "repository discovery", "status": "completed"})
        view.close()
        rendered = output.getvalue()
        self.assertIn("› repository discovery", rendered)
        self.assertIn("✓ repository discovery", rendered)

    def test_setup_table_wraps_without_truncating_proposals(self) -> None:
        ending = "END-OF-CONFIRMED-PROTOCOL"
        rendered = _setup_proposal_table(
            [
                {
                    "id": "trial_protocol",
                    "proposed_answer": "detail " * 40 + ending,
                    "source": "setup brief",
                }
            ]
        )
        self.assertIn(ending, rendered)
        self.assertNotIn("…", rendered)

    def test_setup_error_prints_cause_log_and_retry(self) -> None:
        payload = {
            "state": "ERROR",
            "success": False,
            "message": "Failed: Codex rejected the setup prompt.",
            "next_command": "arctl setup demo",
            "log_path": "/tmp/demo/setup",
            "evidence_valid": True,
        }
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            _emit(payload, as_json=False)
        rendered = output.getvalue()
        self.assertIn("Codex rejected", rendered)
        self.assertIn("Details: /tmp/demo/setup", rendered)
        self.assertIn("Retry: arctl setup demo", rendered)

    def test_setup_interrupt_is_concise_and_resumable(self) -> None:
        with mock.patch("arctl.cli._setup", side_effect=KeyboardInterrupt):
            code, output = self.run_cli(["setup", "demo", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["state"], "SETUP_STOPPED")
        self.assertEqual(payload["allowed_actions"], ["setup", "status"])

    def test_human_status_discloses_ceiling_fallback(self) -> None:
        payload = {
            "schema_version": 1,
            "success": True,
            "task_id": "demo",
            "experiment_id": None,
            "state": "READY",
            "action_required": False,
            "allowed_actions": ["run"],
            "artifacts": [],
            "message": "Ready.",
            "evidence_valid": None,
            "can_continue": True,
            "log_path": None,
            "next_command": "arctl run demo",
            "status": {
                "state": "READY",
                "trial_count": 64,
                "champion": "a" * 40,
                "last_result": None,
                "provisional": False,
                "stop_requested": False,
                "calibration_summary": {
                    "criterion_met": False,
                    "diagnostic": "baseline standard error",
                    "units": "score",
                    "maximum": 3.0,
                    "selected_value": 3.4,
                    "ceiling_fallback": True,
                },
            },
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _emit(payload, as_json=False)
        self.assertIn("approved ceiling was used", output.getvalue())

    def test_real_entrypoint_is_preserved_in_recommended_commands(self) -> None:
        with mock.patch("sys.argv", ["./.venv/bin/arctl", "status"]):
            self.assertEqual(_invoked_program(None), "./.venv/bin/arctl")

        payload = {"next_command": "arctl run demo"}
        _rewrite_next_command(
            payload,
            "./.venv/bin/arctl",
            Path("/tmp/arctl task data"),
        )
        self.assertEqual(
            payload["next_command"],
            "./.venv/bin/arctl --data '/tmp/arctl task data' run demo",
        )

        payload = {"next_command": "arctl approve demo"}
        _rewrite_next_command(
            payload,
            "./.venv/bin/arctl",
            Path("test_tris/.arctl-data"),
        )
        self.assertEqual(
            payload["next_command"],
            "./.venv/bin/arctl --data test_tris/.arctl-data approve demo",
        )

    def test_init_rejects_non_git_repo_and_unsafe_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code, _ = self.run_cli(["--data", str(root / "data"), "init", "--repo", str(root)])
            self.assertEqual(code, 1)

            repo = root / "repo"
            repo.mkdir()
            self.initialize_repo(repo)
            code, _ = self.run_cli(
                [
                    "--data",
                    str(root / "data"),
                    "init",
                    "--repo",
                    str(repo),
                    "--task-id",
                    "../escape",
                ]
            )
            self.assertEqual(code, 1)

    def test_draft_status_report_and_stop_have_safe_json_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "subject"
            repo.mkdir()
            self.initialize_repo(repo)
            data = root / "data"
            code, _ = self.run_cli(
                ["--data", str(data), "init", "--repo", str(repo), "--json"]
            )
            self.assertEqual(code, 0)
            code, output = self.run_cli(
                ["--data", str(data), "status", "subject", "--json"]
            )
            status = json.loads(output)
            self.assertEqual(code, 0)
            self.assertEqual(status["state"], "TASK_DRAFT")
            self.assertEqual(
                status["next_command"],
                f"arctl --data {data} approve subject",
            )

            code, output = self.run_cli(
                ["--data", str(data), "report", "subject", "--json"]
            )
            report = json.loads(output)
            self.assertEqual(code, 0)
            self.assertEqual(report["report"]["completed_experiments"], 0)
            self.assertIn("no task-wide", report["report"]["limitations"])

            for _ in range(2):
                code, output = self.run_cli(
                    ["--data", str(data), "stop", "subject", "--json"]
                )
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output)["state"], "STOP_REQUESTED")

    def test_ctrl_c_requests_the_same_persistent_stop_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "subject"
            repo.mkdir()
            self.initialize_repo(repo)
            data = root / "data"
            self.run_cli(
                ["--data", str(data), "init", "--repo", str(repo), "--json"]
            )
            recovered = {
                "schema_version": 1,
                "success": True,
                "task_id": "subject",
                "experiment_id": None,
                "state": "STOPPED",
                "action_required": False,
                "allowed_actions": ["status"],
                "artifacts": [],
                "message": "Stopped safely.",
                "evidence_valid": None,
                "can_continue": True,
                "log_path": str(data / "tasks" / "subject"),
                "next_command": "arctl status subject",
            }
            with mock.patch(
                "arctl.cli._run",
                side_effect=(KeyboardInterrupt(), recovered),
            ) as run:
                code, output = self.run_cli(
                    ["--data", str(data), "run", "subject", "--json"]
                )

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["state"], "STOPPED")
            self.assertEqual(run.call_count, 2)
            self.assertIsNone(run.call_args_list[0].kwargs["progress"])
            self.assertTrue(
                (data / "tasks" / "subject" / "stop.requested").is_file()
            )

    def test_inspect_infers_task_when_only_experiment_id_is_given(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "subject"
            repo.mkdir()
            self.initialize_repo(repo)
            data = root / "data"
            self.run_cli(
                ["--data", str(data), "init", "--repo", str(repo), "--json"]
            )
            start_experiment(data / "tasks" / "subject", "a" * 40)

            with contextlib.chdir(repo):
                code, output = self.run_cli(
                    ["--data", str(data), "inspect", "1", "--json"]
                )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(payload["task_id"], "subject")
            self.assertEqual(payload["experiment_id"], 1)

    def test_run_refuses_to_start_when_doctor_preflight_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "subject"
            repo.mkdir()
            self.initialize_repo(repo)
            data = root / "data"
            self.run_cli(
                ["--data", str(data), "init", "--repo", str(repo), "--json"]
            )
            with (
                mock.patch(
                    "arctl.doctor.run_doctor",
                    return_value={"subject_profile": False},
                ),
                mock.patch("arctl.runner.run_task") as run,
            ):
                code, output = self.run_cli(
                    ["--data", str(data), "run", "subject", "--json"]
                )

            self.assertEqual(code, 1)
            self.assertIn("preflight failed", json.loads(output)["message"])
            run.assert_not_called()
