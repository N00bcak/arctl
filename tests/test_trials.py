from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arctl.errors import StateError
from arctl.models import TaskConfig
from arctl.trials import freeze_fixed_trial_count, load_trial_count

from .helpers import valid_task


class TrialCountTests(unittest.TestCase):
    def task(self, trials):
        raw = valid_task()
        raw["trials"] = trials
        return TaskConfig.from_mapping(raw)

    def test_fixed_count_is_frozen_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            task = self.task(1)
            freeze_fixed_trial_count(directory, task)
            self.assertEqual(load_trial_count(directory, task), 1)
            path = directory / "trial-count.json"
            raw = json.loads(path.read_text())
            raw["trial_count"] = 2
            path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(StateError, "differs"):
                load_trial_count(directory, task)

    def test_auto_requires_completed_automatic_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            task = self.task("auto")
            freeze_fixed_trial_count(directory, task)
            self.assertFalse((directory / "trial-count.json").exists())
            with self.assertRaisesRegex(StateError, "has not completed"):
                load_trial_count(directory, task)
