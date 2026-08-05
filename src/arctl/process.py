"""Exactly-once managed process execution."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import selectors
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Sequence

from .errors import ProcessError, StateError, StoppedError
from .storage import atomic_write_json

_GATED_EXEC = """\
import os
import sys

descriptor = int(sys.argv[1])
with os.fdopen(descriptor, "rb", closefd=True) as gate:
    released = gate.read(1)
if released != b"1":
    raise SystemExit(125)
os.execvp(sys.argv[2], sys.argv[2:])
"""


def _process_start_time(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = stat[stat.rfind(")") + 2 :].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError) as error:
        raise ProcessError("could not identify managed process") from error


def _kill_recorded_process(directory: Path) -> None:
    path = directory / "process.json"
    if not path.exists():
        return
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("managed process identity is invalid") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "pid", "start_time"}
        or value["schema_version"] != 1
        or isinstance(value["pid"], bool)
        or not isinstance(value["pid"], int)
        or value["pid"] <= 0
        or isinstance(value["start_time"], bool)
        or not isinstance(value["start_time"], int)
        or value["start_time"] <= 0
    ):
        raise StateError("managed process identity is invalid")
    pid = value["pid"]
    try:
        current_start = _process_start_time(pid)
    except ProcessError:
        return
    if current_start != value["start_time"]:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            if _process_start_time(pid) != current_start:
                return
        except ProcessError:
            return
        time.sleep(0.01)


def run_once(
    directory: Path,
    command: Sequence[str],
    *,
    timeout_seconds: float,
    max_output_bytes: int = 1_000_000,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    stop_path: Path | None = None,
    stdin_path: Path | None = None,
) -> dict[str, Any]:
    """Run one argv command, recording start before execution and never rerunning it."""
    if not command or any(not isinstance(argument, str) for argument in command):
        raise ValueError("command must be a non-empty argument vector")
    if timeout_seconds <= 0 or max_output_bytes <= 0:
        raise ValueError("limits must be positive")
    if stop_path is not None and stop_path.exists():
        raise StoppedError("process stopped before it started")

    started = directory / "started.json"
    result = directory / "result.json"
    if started.exists():
        raise StateError("process has already started and cannot be rerun")
    directory.mkdir(parents=True, exist_ok=True)
    stdin_record = _stdin_record(stdin_path)
    atomic_write_json(
        started,
        {
            "schema_version": 2 if stdin_record is not None else 1,
            "command": list(command),
            "cwd": str(cwd.resolve()) if cwd is not None else None,
            "environment": dict(env) if env is not None else None,
            "stop_path": str(stop_path.resolve()) if stop_path is not None else None,
            **({"stdin": stdin_record} if stdin_record is not None else {}),
        },
    )

    stdout_path = directory / "stdout.bin"
    stderr_path = directory / "stderr.bin"
    temporary_directory = (
        Path(tempfile.mkdtemp(prefix="arctl-process-", dir=directory))
        if cwd is None
        else None
    )
    process_directory = temporary_directory if temporary_directory is not None else cwd
    assert process_directory is not None
    process: subprocess.Popen[bytes] | None = None
    gate_read: int | None = None
    gate_write: int | None = None
    try:
        with (
            stdout_path.open("wb") as stdout,
            stderr_path.open("wb") as stderr,
            (
                stdin_path.open("rb")
                if stdin_path is not None
                else open(os.devnull, "rb")
            ) as stdin,
        ):
            gate_read, gate_write = os.pipe()
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _GATED_EXEC,
                    str(gate_read),
                    *command,
                ],
                cwd=process_directory,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=env,
                pass_fds=(gate_read,),
            )
            os.close(gate_read)
            gate_read = None
            atomic_write_json(
                directory / "process.json",
                {
                    "schema_version": 1,
                    "pid": process.pid,
                    "start_time": _process_start_time(process.pid),
                },
            )
            os.write(gate_write, b"1")
            os.close(gate_write)
            gate_write = None
            assert process.stdout is not None
            assert process.stderr is not None
            streams = {
                process.stdout: stdout,
                process.stderr: stderr,
            }
            selector = selectors.DefaultSelector()
            for source in streams:
                selector.register(source, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout_seconds
            output_bytes = 0
            try:
                while selector.get_map():
                    if stop_path is not None and stop_path.exists():
                        raise StoppedError("process stopped by request")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProcessError("process timed out")
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output_bytes += len(chunk)
                        streams[key.fileobj].write(chunk)
                        if output_bytes > max_output_bytes:
                            raise ProcessError(
                                "process output exceeded the configured limit"
                            )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProcessError("process timed out")
                return_code = process.wait(timeout=remaining)
            except (ProcessError, subprocess.TimeoutExpired) as error:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                if isinstance(error, subprocess.TimeoutExpired):
                    raise ProcessError("process timed out") from error
                raise
            finally:
                selector.close()
                process.stdout.close()
                process.stderr.close()

        record = {
            "schema_version": 1,
            "return_code": return_code,
            "stdout_bytes": stdout_path.stat().st_size,
            "stderr_bytes": stderr_path.stat().st_size,
        }
        atomic_write_json(result, record)
        return record
    finally:
        if gate_read is not None:
            os.close(gate_read)
        if gate_write is not None:
            os.close(gate_write)
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        if temporary_directory is not None:
            shutil.rmtree(temporary_directory)


def read_valid_result(directory: Path) -> dict[str, Any]:
    path = directory / "result.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("process has no valid result") from error
    expected = {"schema_version", "return_code", "stdout_bytes", "stderr_bytes"}
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value["schema_version"] != 1
        or any(
            isinstance(value[field], bool) or not isinstance(value[field], int)
            for field in ("return_code", "stdout_bytes", "stderr_bytes")
        )
    ):
        raise StateError("process has no valid result")
    return value


def run_or_load_once(
    directory: Path,
    command: Sequence[str],
    *,
    timeout_seconds: float,
    max_output_bytes: int,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    stop_path: Path | None = None,
    stdin_path: Path | None = None,
) -> dict[str, Any]:
    stdin_record = _stdin_record(stdin_path)
    expected_started = {
        "schema_version": 2 if stdin_record is not None else 1,
        "command": list(command),
        "cwd": str(cwd.resolve()),
        "environment": dict(env) if env is not None else None,
        "stop_path": str(stop_path.resolve()) if stop_path is not None else None,
        **({"stdin": stdin_record} if stdin_record is not None else {}),
    }
    started_path = directory / "started.json"
    if (directory / "result.json").exists():
        try:
            started = json.loads(started_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("process result has no valid started record") from error
        if started != expected_started:
            raise StateError("saved process does not match the reserved command")
        return read_valid_result(directory)
    if started_path.exists():
        _kill_recorded_process(directory)
        raise StateError("process started without a valid result and cannot be rerun")
    return run_once(
        directory,
        command,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        cwd=cwd,
        env=env,
        stop_path=stop_path,
        stdin_path=stdin_path,
    )


def _stdin_record(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if path.is_symlink() or not path.is_file():
        raise StateError("process stdin must be one regular file")
    content = path.read_bytes()
    return {
        "path": str(path.resolve()),
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
