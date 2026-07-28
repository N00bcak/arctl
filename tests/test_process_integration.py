from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from arctl.errors import ProcessError, StateError, StoppedError
from arctl.process import read_valid_result, run_once, run_or_load_once


class ProcessIntegrationTests(unittest.TestCase):
    def test_records_real_process_and_refuses_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary, "process")
            result = run_once(
                directory,
                ["python3", "-c", "print('ok')"],
                timeout_seconds=2,
            )
            self.assertEqual(result["return_code"], 0)
            self.assertEqual(read_valid_result(directory), result)
            self.assertEqual((directory / "stdout.bin").read_text(), "ok\n")
            with self.assertRaisesRegex(StateError, "cannot be rerun"):
                run_once(
                    directory,
                    ["python3", "-c", "print('second')"],
                    timeout_seconds=2,
                )

    def test_discards_process_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary, "process")
            result = run_once(
                directory,
                ["python3", "-c", "open('scratch.txt', 'w').write('temporary')"],
                timeout_seconds=2,
            )
            self.assertEqual(result["return_code"], 0)
            self.assertEqual(list(directory.glob("arctl-process-*")), [])

    def test_runs_in_explicit_working_directory_and_loads_saved_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cwd = root / "cwd"
            cwd.mkdir()
            directory = root / "process"
            command = [
                "python3",
                "-c",
                "from pathlib import Path; Path('seen').write_text('yes')",
            ]
            first = run_or_load_once(
                directory,
                command,
                timeout_seconds=2,
                max_output_bytes=1000,
                cwd=cwd,
            )
            second = run_or_load_once(
                directory,
                command,
                timeout_seconds=2,
                max_output_bytes=1000,
                cwd=cwd,
            )
            self.assertEqual(first, second)
            self.assertEqual((cwd / "seen").read_text(), "yes")
            with self.assertRaisesRegex(StateError, "reserved command"):
                run_or_load_once(
                    directory,
                    ["python3", "-c", "pass"],
                    timeout_seconds=2,
                    max_output_bytes=1000,
                    cwd=cwd,
                )

    def test_timeout_kills_descendant_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary, "process")
            pid_file = Path(temporary, "child.pid")
            script = (
                "import pathlib, subprocess, sys, time;"
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']);"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid));"
                "time.sleep(30)"
            )
            with self.assertRaisesRegex(ProcessError, "timed out"):
                run_once(
                    directory,
                    ["python3", "-c", script],
                    timeout_seconds=0.5,
                )
            child_pid = int(pid_file.read_text())
            for _ in range(20):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
                self.assertEqual(state, "Z", "descendant process remained alive")
            self.assertFalse((directory / "result.json").exists())
            with self.assertRaisesRegex(StateError, "cannot be rerun"):
                run_once(
                    directory,
                    ["python3", "-c", "pass"],
                    timeout_seconds=1,
                )

    def test_output_limit_invalidates_completed_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary, "process")
            with self.assertRaisesRegex(ProcessError, "output exceeded"):
                run_once(
                    directory,
                    ["python3", "-c", "print('x' * 100)"],
                    timeout_seconds=2,
                    max_output_bytes=10,
                )
            self.assertFalse((directory / "result.json").exists())

    def test_output_limit_stops_a_process_that_keeps_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary, "process")
            started = time.monotonic()
            with self.assertRaisesRegex(ProcessError, "output exceeded"):
                run_once(
                    directory,
                    [
                        "python3",
                        "-c",
                        "import sys,time; print('x' * 1000); sys.stdout.flush(); time.sleep(10)",
                    ],
                    timeout_seconds=5,
                    max_output_bytes=10,
                )
            self.assertLess(time.monotonic() - started, 2)

    def test_stop_marker_kills_process_group_without_valid_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "process"
            stop = root / "stop.requested"
            timer = threading.Timer(0.2, stop.write_text, args=("{}",))
            timer.start()
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(StoppedError, "stopped"):
                    run_once(
                        directory,
                        ["python3", "-c", "import time; time.sleep(30)"],
                        timeout_seconds=5,
                        stop_path=stop,
                    )
            finally:
                timer.cancel()
                timer.join()
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue((directory / "started.json").is_file())
            self.assertFalse((directory / "result.json").exists())

    def test_recovery_kills_exact_process_group_left_by_hard_controller_crash(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "process"
            command = [
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            ]
            controller = """\
import sys
from pathlib import Path
from arctl.process import run_once

run_once(
    Path(sys.argv[1]),
    [sys.executable, "-c", "import time; time.sleep(30)"],
    timeout_seconds=60,
    cwd=Path(sys.argv[2]),
)
"""
            parent = subprocess.Popen(
                [sys.executable, "-c", controller, str(directory), str(root)]
            )
            identity = directory / "process.json"
            for _ in range(100):
                if identity.is_file():
                    break
                time.sleep(0.02)
            else:
                parent.kill()
                parent.wait()
                self.fail("controller did not durably record its child")
            child_pid = json.loads(identity.read_text())["pid"]
            os.kill(parent.pid, signal.SIGKILL)
            parent.wait()

            with self.assertRaisesRegex(StateError, "cannot be rerun"):
                run_or_load_once(
                    directory,
                    command,
                    timeout_seconds=60,
                    max_output_bytes=1000,
                    cwd=root,
                )

            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                return
            state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
            self.assertEqual(state, "Z", "orphaned official process remained alive")

    def test_rejects_corrupt_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "result.json").write_text(json.dumps({"return_code": 0}))
            with self.assertRaisesRegex(StateError, "no valid result"):
                read_valid_result(directory)
