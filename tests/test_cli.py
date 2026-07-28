from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arctl.cli import main
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
            self.assertEqual(payload["next_command"], "arctl approve subject")
            task = root / "data" / "tasks" / "subject" / "task.yaml"
            self.assertIn(f'repo: "{repo}"', task.read_text())

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
            self.assertIn("Valid saved evidence remains unchanged", error["message"])
            self.assertTrue(error["evidence_valid"])
            self.assertFalse(error["can_continue"])
            self.assertIn("log_path", error)

    def test_human_output_ends_with_exactly_one_next_command(self) -> None:
        code, output = self.run_cli(["doctor"])
        self.assertIn(code, (0, 1))
        next_lines = [line for line in output.splitlines() if line.startswith("Next: ")]
        self.assertEqual(len(next_lines), 1)

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
            self.assertEqual(status["next_command"], "arctl approve subject")

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
