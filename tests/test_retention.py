from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import sys
from unittest import mock
from pathlib import Path

from arctl.errors import StateError
from arctl.bytecode import ensure_experiment_bytecode_cache
from arctl.retention import (
    build_gc_plan,
    build_experiment_gc_plan,
    canonical_snapshot,
    experiment_canonical_snapshot,
    recover_gc_transaction,
    run_experiment_gc,
    run_gc,
)
from arctl.setup import _tree_hash


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


class RetentionTests(unittest.TestCase):
    def make_completed_cache(self, task: Path) -> Path:
        (task / "task.yaml").write_text("task_id: demo\n")
        home = task / "setup" / "fixture" / "output"
        cache = home / "pycache"
        cache.mkdir(parents=True)
        (cache / "module.pyc").write_bytes(b"compiled")
        process = task / "setup" / "fixture" / "process"
        write_json(
            process / "started.json",
            {"schema_version": 1, "command": ["true"], "cwd": str(task),
             "environment": {"HOME": str(home)}, "stop_path": None},
        )
        write_json(
            process / "result.json",
            {"schema_version": 2, "return_code": 0, "stdout_bytes": 0,
             "stderr_bytes": 0, "elapsed_seconds": 0.1},
        )
        write_json(
            process / "process.json",
            {"schema_version": 2, "pid": 999999, "pgid": 999999,
             "platform": "Darwin", "start_time": 1},
        )
        return cache

    def test_dry_run_and_mutation_share_the_same_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary, "demo")
            task.mkdir()
            cache = self.make_completed_cache(task)
            before = canonical_snapshot(task)

            dry = run_gc(task, dry_run=True)
            applied = run_gc(task, dry_run=False)

            self.assertEqual(dry["plan_hash"], applied["plan_hash"])
            self.assertEqual(dry["actions"], applied["actions"])
            self.assertFalse(cache.exists())
            self.assertGreater(applied["reclaimed_bytes"], 0)
            self.assertEqual(canonical_snapshot(task), before)
            second = run_gc(task, dry_run=False)
            self.assertEqual(second["eligible_bytes"], 0)
            self.assertFalse(second["mutation_occurred"])

    def test_malformed_started_record_cannot_claim_an_arbitrary_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary, "demo")
            task.mkdir()
            (task / "task.yaml").write_text("task_id: demo\n")
            valuable = task / "valuable"
            cache = valuable / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "preserve.pyc").write_bytes(b"valuable")
            forged = task / "forged" / "process"
            write_json(forged / "started.json", {"environment": {"HOME": str(valuable)}})
            write_json(
                forged / "result.json",
                {"schema_version": 2, "return_code": 0, "stdout_bytes": 0,
                 "stderr_bytes": 0, "elapsed_seconds": 0.1},
            )

            plan = build_gc_plan(task)

            claimed = cache.relative_to(task).as_posix()
            self.assertNotIn(
                claimed,
                {
                    item
                    for action in plan["actions"]
                    for item in action["inputs"]
                },
            )
            self.assertTrue(cache.exists())

    def test_scoped_gc_removes_only_a_published_experiment_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary, "demo")
            experiment = task / "experiments" / "000001"
            experiment.mkdir(parents=True)
            (task / "task.yaml").write_text("task_id: demo\n")
            write_json(task / "approval.json", {"schema_version": 1})
            write_json(task / "evaluator.manifest.json", {"schema_version": 1})
            write_json(experiment / "experiment.json", {"state": "COMPLETE"})
            write_json(experiment / "request.public.json", {"claim": "one"})
            write_json(experiment / "result.public.json", {"decision": "REJECT"})
            (experiment / "published").touch()
            write_json(
                task / "exploration" / "entries" / "000001.public.json",
                {"source": "experiment:000001"},
            )
            cache = ensure_experiment_bytecode_cache(
                task, experiment, python_executable=sys.executable
            )
            (cache / "module.pyc").write_bytes(b"compiled")
            unrelated = task / "experiments" / "000002" / "runtime" / "keep"
            unrelated.mkdir(parents=True)
            (unrelated / "data").write_text("keep")
            before = experiment_canonical_snapshot(task, experiment)

            result = run_experiment_gc(task, experiment)

            self.assertFalse(result["failed"])
            self.assertEqual(result["scope"], {"kind": "experiment", "id": "000001"})
            self.assertFalse(cache.exists())
            self.assertEqual((unrelated / "data").read_text(), "keep")
            self.assertEqual(experiment_canonical_snapshot(task, experiment), before)

    def test_scoped_gc_recovers_deletion_before_journal_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary, "demo")
            experiment = task / "experiments" / "000001"
            experiment.mkdir(parents=True)
            (task / "task.yaml").write_text("task_id: demo\n")
            write_json(task / "approval.json", {"schema_version": 1})
            write_json(task / "evaluator.manifest.json", {"schema_version": 1})
            write_json(experiment / "experiment.json", {"state": "COMPLETE"})
            write_json(experiment / "request.public.json", {"claim": "one"})
            write_json(experiment / "result.public.json", {"decision": "REJECT"})
            (experiment / "published").touch()
            write_json(
                task / "exploration" / "entries" / "000001.public.json",
                {"source": "experiment:000001"},
            )
            cache = ensure_experiment_bytecode_cache(
                task, experiment, python_executable=sys.executable
            )
            (cache / "module.pyc").write_bytes(b"compiled")
            plan = build_experiment_gc_plan(task, experiment)
            quarantine_action = next(
                action
                for action in plan["actions"]
                if action["type"] == "quarantine_disposable_root"
            )
            remove_action = next(
                action
                for action in plan["actions"]
                if action["type"] == "remove_quarantined_root"
            )
            cache_target = (
                task
                / ".gc"
                / "quarantine"
                / plan["plan_hash"]
                / quarantine_action["action_id"]
            )
            cache_target.parent.mkdir(parents=True)
            cache.rename(cache_target)
            shutil.rmtree(cache_target)
            execution = {
                action["action_id"]: action["initial_status"]
                for action in plan["actions"]
            }
            execution[quarantine_action["action_id"]] = "quarantined"
            execution[remove_action["action_id"]] = "conditional"
            write_json(
                task / ".gc" / "transaction.json",
                {"schema_version": 1, "plan": plan, "execution": execution},
            )

            recovered = recover_gc_transaction(task)

            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertFalse(recovered["failed"])
            self.assertEqual(
                recovered["execution"][remove_action["action_id"]], "removed"
            )
            self.assertFalse((task / ".gc" / "transaction.json").exists())

    def test_incomplete_stage_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary, "demo")
            task.mkdir()
            cache = self.make_completed_cache(task)
            (task / "stage" / "process" / "result.json").unlink()

            plan = build_gc_plan(task)

            cache_actions = [
                item for item in plan["actions"]
                if item["rule_id"] == "managed-python-cache"
            ]
            self.assertEqual(cache_actions[0]["initial_status"], "blocked")
            run_gc(task, dry_run=False)
            self.assertTrue(cache.exists())

    def test_symlinked_cache_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary, "demo")
            task.mkdir()
            cache = self.make_completed_cache(task)
            target = task / "outside"
            target.write_text("keep")
            (cache / "link").symlink_to(target)

            result = run_gc(task, dry_run=True)

            self.assertEqual(result["eligible_bytes"], 0)
            self.assertTrue(any("symlink" in item["reason"] for item in result["skipped"]))
            self.assertEqual(target.read_text(), "keep")

    def test_recovers_a_rename_before_journal_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary, "demo")
            task.mkdir()
            cache = self.make_completed_cache(task)
            plan = build_gc_plan(task)
            quarantine_action = next(
                item for item in plan["actions"]
                if item["type"] == "quarantine_disposable_root"
            )
            target = task / ".gc" / "quarantine" / plan["plan_hash"] / quarantine_action["action_id"]
            target.parent.mkdir(parents=True)
            cache.rename(target)
            execution = {item["action_id"]: item["initial_status"] for item in plan["actions"]}
            write_json(
                task / ".gc" / "transaction.json",
                {"schema_version": 1, "plan": plan, "execution": execution},
            )

            result = run_gc(task, dry_run=False)

            self.assertFalse(result["failed"])
            self.assertFalse(target.exists())
            self.assertFalse((task / ".gc" / "transaction.json").exists())

    def test_removes_only_recognized_terminal_scratch_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary, "demo")
            task.mkdir()
            self.make_completed_cache(task)
            home = task / "stage" / "output"
            synthetic = home / "tmp" / "arg0" / "codex-arg0AbC123"
            synthetic.mkdir(parents=True)
            (synthetic / ".lock").touch()
            for name in ("applypatch", "apply_patch", "codex-execve-wrapper"):
                (synthetic / name).symlink_to("/opt/homebrew/bin/codex")
            unrelated = home / "unrelated"
            unrelated.mkdir()
            (unrelated / "keep").write_text("keep")

            result = run_gc(task, dry_run=False)

            self.assertFalse(result["failed"])
            self.assertFalse(synthetic.exists())
            self.assertEqual((unrelated / "keep").read_text(), "keep")

    def test_mutation_revalidates_source_after_journal_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary, "demo")
            task.mkdir()
            cache = self.make_completed_cache(task)
            from arctl import retention

            original = retention._save_journal
            calls = 0

            def save_then_change(path, value):
                nonlocal calls
                original(path, value)
                calls += 1
                if calls == 1:
                    (cache / "late.pyc").write_bytes(b"changed")

            with mock.patch.object(retention, "_save_journal", save_then_change):
                result = run_gc(task, dry_run=False)

            self.assertTrue(result["failed"])
            self.assertTrue(cache.exists())
            self.assertTrue((task / ".gc" / "transaction.json").is_file())

    def init_repo(self, path: Path) -> str:
        path.mkdir()
        subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
        (path / "owned.txt").write_text("owned\n")
        subprocess.run(["git", "-C", str(path), "add", "owned.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(path), "-c", "user.name=test", "-c",
             "user.email=test@example.invalid", "commit", "-qm", "initial"],
            check=True,
        )
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def test_setup_staging_blocks_contradictory_runtime_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            task = workspace / "data" / "tasks" / "demo"
            task.mkdir(parents=True)
            (task / "task.yaml").write_text("task_id: demo\n")
            write_json(task / "approval.json", {"schema_version": 1})
            repos = {name: workspace / name for name in ("subject", "environment", "evaluator")}
            commits = {name: self.init_repo(path) for name, path in repos.items()}
            attempt = task / "setup" / "staging" / "0001"
            roots = {name: attempt / name for name in (*repos, "runtime")}
            for root in roots.values():
                root.mkdir(parents=True)
            for name in repos:
                (roots[name] / "owned.txt").write_text("owned\n")
            project = b'[project]\nname="runtime"\nversion="0"\ndependencies=[]\n'
            lock = b'version = 1\nrevision = 1\nrequires-python = ">=3.11"\n'
            (roots["runtime"] / "pyproject.toml").write_bytes(project)
            (roots["runtime"] / "uv.lock").write_bytes(lock)
            owned: dict[str, list[object]] = {}
            readiness = {
                "schema_version": 2,
                "staging": {name: str(path) for name, path in roots.items()},
                "dependency_lock_sha256": hashlib.sha256(lock).hexdigest(),
                "dependency_resolution": {
                    "schema_version": 1,
                    "name": "uv",
                    "version": "0.8.14",
                    "index": "https://pypi.org/simple",
                    "options": ["sync", "--no-config"],
                },
                "owned_files": owned,
                "owned_files_sha256": hashlib.sha256(
                    json.dumps(owned, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "tree_hashes": {
                    name: _tree_hash(roots[name], exclude_private=name == "evaluator")
                    for name in repos
                },
            }
            write_json(task / "setup" / "readiness.public.json", readiness)
            write_json(
                task / "setup.json",
                {"workspace": str(workspace), **{name: str(path) for name, path in repos.items()},
                 **{f"{name}_commit": commit for name, commit in commits.items()}},
            )
            venv = workspace / ".venv"
            (venv / "bin").mkdir(parents=True)
            (venv / "pyvenv.cfg").write_text(
                "implementation = CPython\nversion_info = 3.13.7\nuv = 0.8.14\n"
            )
            interpreter = venv / "bin" / "python"
            interpreter.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '{\"abi\":\"cpython-312-darwin\","
                "\"cache_tag\":\"cpython-312\",\"executable\":\""
                + str(interpreter)
                + "\",\"implementation\":\"CPython\",\"platform\":\"darwin\","
                "\"version\":\"3.13.7\"}'\n"
            )
            interpreter.chmod(0o755)

            with mock.patch(
                "arctl.retention._validate_approval_bundle", return_value={}
            ):
                plan = build_gc_plan(task)

            setup_actions = [
                action for action in plan["actions"]
                if action["rule_id"] in {
                    "runtime-dependency-provenance", "completed-setup-staging"
                }
            ]
            self.assertNotIn(
                "promote_dependency_provenance",
                {action["type"] for action in setup_actions},
            )
            self.assertTrue(
                any(
                    action["initial_status"] == "blocked"
                    and "runtime" in action["reason"]
                    for action in setup_actions
                ),
                setup_actions,
            )
            self.assertTrue((task / "setup" / "staging").exists())

            interpreter.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '{\"abi\":\"cpython-313-darwin\","
                "\"cache_tag\":\"cpython-313\",\"executable\":\""
                + str(interpreter)
                + "\",\"implementation\":\"CPython\",\"platform\":\"darwin\","
                "\"version\":\"3.13.7\"}'\n"
            )
            with mock.patch(
                "arctl.retention._validate_approval_bundle", return_value={}
            ):
                consistent = build_gc_plan(task)
            setup_actions = [
                action for action in consistent["actions"]
                if action["rule_id"] in {
                    "runtime-dependency-provenance", "completed-setup-staging"
                }
            ]
            self.assertIn(
                "promote_dependency_provenance",
                {action["type"] for action in setup_actions},
            )
            self.assertFalse(
                any(action["initial_status"] == "blocked" for action in setup_actions),
                setup_actions,
            )
            with mock.patch(
                "arctl.retention._validate_approval_bundle", return_value={}
            ):
                applied = run_gc(task, dry_run=False)
            self.assertFalse(applied["failed"], applied)
            self.assertFalse((task / "setup" / "staging").exists())
            provenance = task / "setup" / "dependency-provenance"
            self.assertEqual((provenance / "uv.lock").read_bytes(), lock)
            manifest = json.loads((provenance / "manifest.public.json").read_text())
            self.assertEqual(manifest["python"]["cache_tag"], "cpython-313")
            self.assertEqual(manifest["schema_version"], 2)


if __name__ == "__main__":
    unittest.main()
