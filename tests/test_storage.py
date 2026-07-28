from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arctl.errors import StateError
from arctl.storage import TaskLock, atomic_write_json, write_json_once


class StorageTests(unittest.TestCase):
    def test_atomic_json_replaces_complete_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "state.json")
            atomic_write_json(path, {"generation": 1, "payload": "old"})
            atomic_write_json(path, {"generation": 2})
            self.assertEqual(json.loads(path.read_text()), {"generation": 2})
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_failed_serialization_preserves_previous_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "state.json")
            atomic_write_json(path, {"generation": 1})
            with self.assertRaises(TypeError):
                atomic_write_json(path, {"not_json": object()})
            self.assertEqual(json.loads(path.read_text()), {"generation": 1})
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_task_lock_is_exclusive_and_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "lock")
            with TaskLock(path):
                with self.assertRaisesRegex(StateError, "already locked"):
                    with TaskLock(path):
                        pass
            with TaskLock(path):
                pass

    def test_immutable_json_record_accepts_only_identical_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "request.json")
            write_json_once(path, {"operation": "prepare", "count": 4})
            write_json_once(path, {"operation": "prepare", "count": 4})
            with self.assertRaisesRegex(StateError, "changed"):
                write_json_once(path, {"operation": "prepare", "count": 5})
