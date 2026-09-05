from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arctl.experiment import start_experiment
from arctl.operations import inspect_experiment, task_status
from arctl.registry import LocatedTask

from .helpers import valid_task
from arctl.models import TaskConfig


class OperationsTests(unittest.TestCase):
    def test_inspect_exposes_inventory_but_not_private_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_directory = root / "task"
            task_directory.mkdir()
            config = TaskConfig.from_mapping(valid_task())
            experiment, record = start_experiment(task_directory, "a" * 40)
            (experiment / "request.public.json").write_text("{}")
            (experiment / "private").mkdir()
            (experiment / "private" / "seed.json").write_text(
                json.dumps({"master_seed": "do-not-disclose"})
            )
            located = LocatedTask(task_directory, config)

            inspected = inspect_experiment(located, record.experiment_id)

            self.assertEqual(inspected["experiment"]["state"], "RESEARCHING")
            artifacts = {
                artifact["path"]: artifact["visibility"]
                for artifact in inspected["artifacts"]
            }
            self.assertEqual(artifacts["request.public.json"], "public")
            self.assertEqual(artifacts["private/seed.json"], "private")
            self.assertNotIn("do-not-disclose", json.dumps(inspected))

    def test_status_reports_active_experiment_without_reading_private_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_directory = root / "task"
            task_directory.mkdir()
            config = TaskConfig.from_mapping(valid_task())
            start_experiment(task_directory, "b" * 40)

            status = task_status(LocatedTask(task_directory, config))

            self.assertEqual(status["state"], "RESEARCHING")
            self.assertEqual(status["experiment_id"], 1)
            self.assertFalse(status["approved"])

    def test_status_distinguishes_preserved_research_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_directory = root / "task"
            task_directory.mkdir()
            config = TaskConfig.from_mapping(valid_task())
            experiment, _ = start_experiment(task_directory, "b" * 40)
            (experiment / "research.failure.json").write_text(
                json.dumps(
                    {
                        "message": "fresh research session exited unsuccessfully",
                    }
                )
            )

            status = task_status(LocatedTask(task_directory, config))

            self.assertEqual(status["state"], "RESEARCH_FAILED")
            self.assertEqual(status["experiment_id"], 1)
            self.assertEqual(status["log_path"], str(experiment / "process"))

    def test_status_distinguishes_preserved_planning_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_directory = Path(temporary) / "task"
            attempt = task_directory / "searches" / "000001" / "attempts" / "01"
            attempt.mkdir(parents=True)
            (attempt / "planning.failure.json").write_text(
                json.dumps({"message": "planner failed"})
            )
            process = attempt / "planning" / "attempts" / "0001" / "process"
            process.mkdir(parents=True)
            config = TaskConfig.from_mapping(valid_task())

            status = task_status(LocatedTask(task_directory, config))

            self.assertEqual(status["state"], "PLANNING_FAILED")
            self.assertEqual(status["log_path"], str(process))

    def test_status_distinguishes_preserved_public_check_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_directory = root / "task"
            task_directory.mkdir()
            config = TaskConfig.from_mapping(valid_task())
            experiment, _ = start_experiment(task_directory, "b" * 40)
            (experiment / "public-check.failure.json").write_text(
                json.dumps(
                    {
                        "check": 1,
                        "message": "public-check sandbox did not start its command",
                    }
                )
            )

            status = task_status(LocatedTask(task_directory, config))

            self.assertEqual(status["state"], "PUBLIC_CHECK_FAILED")
            self.assertEqual(status["experiment_id"], 1)
            self.assertEqual(status["log_path"], str(experiment / "process"))
