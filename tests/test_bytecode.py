from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from arctl.bytecode import (
    ensure_experiment_bytecode_cache,
    invalidate_worktree_bytecode,
    mirrored_source_root,
)
from arctl.errors import StateError


class ExperimentBytecodeTests(unittest.TestCase):
    def make_experiment(self, task: Path, identifier: int, claim: str) -> Path:
        experiment = task / "experiments" / f"{identifier:06d}"
        experiment.mkdir(parents=True)
        (experiment / "request.public.json").write_text(
            json.dumps({"claim": claim}) + "\n", encoding="utf-8"
        )
        return experiment

    def make_task(self, root: Path) -> Path:
        task = root / "task"
        task.mkdir()
        (task / "approval.json").write_text('{}\n')
        (task / "evaluator.manifest.json").write_text('{}\n')
        return task

    def run_import(self, source: Path, cache: Path) -> str:
        environment = os.environ.copy()
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        environment["PYTHONPYCACHEPREFIX"] = str(cache)
        completed = subprocess.run(
            (sys.executable, "-c", "import sample; print(sample.VALUE)"),
            cwd=source,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def test_experiments_have_distinct_cache_namespaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = self.make_task(Path(temporary))
            first = self.make_experiment(task, 1, "first")
            second = self.make_experiment(task, 2, "second")

            first_cache = ensure_experiment_bytecode_cache(
                task, first, python_executable="python3"
            )
            second_cache = ensure_experiment_bytecode_cache(
                task, second, python_executable=sys.executable
            )

            self.assertNotEqual(first_cache, second_cache)
            self.assertEqual(first_cache.parent.parent.parent, first.resolve())
            self.assertEqual(second_cache.parent.parent.parent, second.resolve())
            first_manifest = json.loads(
                (first / "runtime" / "bytecode-cache.private.json").read_text()
            )
            self.assertEqual(
                first_manifest["python"]["executable"],
                str(Path(shutil.which("python3") or "").resolve()),
            )

            (first / "request.public.json").write_text(
                json.dumps({"claim": "changed"}) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(StateError, "provenance changed"):
                ensure_experiment_bytecode_cache(
                    task, first, python_executable="python3"
                )

    def test_processes_share_and_reuse_one_compiled_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = self.make_task(root)
            experiment = self.make_experiment(task, 1, "shared")
            cache = ensure_experiment_bytecode_cache(
                task, experiment, python_executable=sys.executable
            )
            source = root / "candidate"
            source.mkdir()
            (source / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")

            environment = os.environ.copy()
            environment.pop("PYTHONDONTWRITEBYTECODE", None)
            environment["PYTHONPYCACHEPREFIX"] = str(cache)
            workers = [
                subprocess.Popen(
                    (sys.executable, "-c", "import sample; print(sample.VALUE)"),
                    cwd=source,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(4)
            ]
            for worker in workers:
                stdout, stderr = worker.communicate(timeout=10)
                self.assertEqual(worker.returncode, 0, stderr)
                self.assertEqual(stdout.strip(), "1")
            compiled = list(cache.rglob("sample.*.pyc"))
            self.assertEqual(len(compiled), 1)
            first_stat = compiled[0].stat()
            self.assertEqual(self.run_import(source, cache), "1")

            self.assertEqual(list(cache.rglob("sample.*.pyc")), compiled)
            self.assertEqual(compiled[0].stat().st_mtime_ns, first_stat.st_mtime_ns)

    def test_worktree_invalidation_prevents_same_timestamp_stale_bytecode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = self.make_task(root)
            experiment = self.make_experiment(task, 1, "invalidate")
            cache = ensure_experiment_bytecode_cache(
                task, experiment, python_executable=sys.executable
            )
            source = root / "candidate"
            source.mkdir()
            module = source / "sample.py"
            module.write_text("VALUE = 1\n", encoding="utf-8")
            fixed_time = 1_700_000_000
            os.utime(module, (fixed_time, fixed_time))
            self.assertEqual(self.run_import(source, cache), "1")

            module.write_text("VALUE = 2\n", encoding="utf-8")
            os.utime(module, (fixed_time, fixed_time))
            invalidate_worktree_bytecode(cache, source)

            self.assertEqual(self.run_import(source, cache), "2")

    def test_candidate_and_champion_use_distinct_mirrors_in_one_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = self.make_task(root)
            experiment = self.make_experiment(task, 1, "paired")
            cache = ensure_experiment_bytecode_cache(
                task, experiment, python_executable=sys.executable
            )
            champion = root / "champion"
            candidate = root / "candidate"
            champion.mkdir()
            candidate.mkdir()
            (champion / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
            (candidate / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")

            self.assertEqual(self.run_import(champion, cache), "1")
            self.assertEqual(self.run_import(candidate, cache), "2")

            champion_mirror = mirrored_source_root(cache, champion)
            candidate_mirror = mirrored_source_root(cache, candidate)
            self.assertNotEqual(champion_mirror, candidate_mirror)
            self.assertEqual(len(list(champion_mirror.rglob("sample.*.pyc"))), 1)
            self.assertEqual(len(list(candidate_mirror.rglob("sample.*.pyc"))), 1)


if __name__ == "__main__":
    unittest.main()
