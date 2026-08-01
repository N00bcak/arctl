from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arctl.cli import (
    _ProgressView,
    _emit,
    _invoked_program,
    _progress,
    _rewrite_next_command,
    main,
)
from arctl.experiment import start_experiment


class CliTests(unittest.TestCase):
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
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
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
            self.assertIn("schema_version: 2", task_text)
            self.assertIn("model: gpt-5.6-sol", task_text)
            self.assertIn("model: gpt-5.6-terra", task_text)

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

    def test_human_output_omits_machine_next_command(self) -> None:
        code, output = self.run_cli(["doctor"])
        self.assertIn(code, (0, 1))
        next_lines = [line for line in output.splitlines() if line.startswith("Next: ")]
        self.assertEqual(next_lines, [])

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
                "code": "exact_duplicate",
                "message": "same candidate was already tested",
            }
        )
        view.close()
        rendered = output.getvalue()
        self.assertIn("Strategy · revision 1", rendered)
        self.assertIn("candidate search · attempt 1/6", rendered)
        self.assertIn("Miss: same candidate was already tested", rendered)

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
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
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
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
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
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
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
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
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
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
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
