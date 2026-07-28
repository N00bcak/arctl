from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arctl.errors import StateError
from arctl.models import TaskConfig
from arctl.registry import locate_task

from .helpers import valid_task


class RegistryTests(unittest.TestCase):
    def test_infers_exactly_one_task_for_current_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks = root / "tasks"
            (tasks / "one").mkdir(parents=True)
            (tasks / "one" / "task.yaml").touch()
            raw = valid_task()
            raw["repo"] = str(root / "subject")
            task = TaskConfig.from_mapping(raw)
            with patch("arctl.registry.load_task", return_value=task):
                located = locate_task(
                    root,
                    task_id=None,
                    current_directory=root / "subject" / "src",
                )
            self.assertEqual(located.config, task)

    def test_requires_explicit_id_when_multiple_tasks_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks = root / "tasks"
            for name in ("one", "two"):
                (tasks / name).mkdir(parents=True)
                (tasks / name / "task.yaml").touch()
            raw = valid_task()
            raw["repo"] = str(root / "subject")
            task = TaskConfig.from_mapping(raw)
            with patch("arctl.registry.load_task", return_value=task):
                with self.assertRaisesRegex(StateError, "multiple tasks"):
                    locate_task(
                        root,
                        task_id=None,
                        current_directory=root / "subject",
                    )
