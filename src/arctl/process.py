"""Exactly-once managed process execution."""

from __future__ import annotations

import hashlib
import json
import os
import platform
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
from .platform_process import inspect_process
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


def _kill_recorded_process(directory: Path) -> None:
    path = directory / "process.json"
    if not path.exists():
        return
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("managed process identity is invalid") from error
    legacy_fields = {"schema_version", "pid", "start_time"}
    current_fields = legacy_fields | {"platform", "pgid"}
    if not isinstance(value, dict) or value.get("schema_version") not in {1, 2}:
        raise StateError("managed process identity is invalid")
    version = value["schema_version"]
    if set(value) != (legacy_fields if version == 1 else current_fields):
        raise StateError("managed process identity is invalid")
    integer_fields = ("pid", "start_time") if version == 1 else (
        "pid",
        "pgid",
        "start_time",
    )
    if any(
        isinstance(value[field], bool)
        or not isinstance(value[field], int)
        or value[field] <= 0
        for field in integer_fields
    ):
        raise StateError("managed process identity is invalid")
    if version == 2 and value["platform"] not in {"Linux", "Darwin"}:
        raise StateError("managed process identity is invalid")
    if version == 1 and platform.system() != "Linux":
        raise StateError("managed process identity schema 1 is supported only on Linux")
    pid = value["pid"]
    current = inspect_process(pid)
    if current is None or not current.alive:
        return
    if version == 1:
        recorded_pgid = pid
    else:
        if current.platform != value["platform"]:
            return
        recorded_pgid = value["pgid"]
    if (
        current.start_time != value["start_time"]
        or current.pgid != recorded_pgid
    ):
        return
    try:
        os.killpg(recorded_pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        observed = inspect_process(pid)
        if (
            observed is None
            or not observed.alive
            or observed.start_time != current.start_time
        ):
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
    monotonic_started = time.monotonic()
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
            identity = inspect_process(process.pid)
            if identity is None or not identity.alive:
                raise ProcessError("could not identify managed process")
            atomic_write_json(
                directory / "process.json",
                {
                    "schema_version": 2,
                    "platform": identity.platform,
                    "pid": identity.pid,
                    "pgid": identity.pgid,
                    "start_time": identity.start_time,
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
            process_exited_at: float | None = None
            try:
                while selector.get_map():
                    if stop_path is not None and stop_path.exists():
                        raise StoppedError("process stopped by request")
                    now = time.monotonic()
                    if process.poll() is not None:
                        if process_exited_at is None:
                            process_exited_at = now
                        elif now - process_exited_at >= 1:
                            # A managed command must not leave descendants running.
                            # Descendants that inherit stdout/stderr otherwise keep the
                            # selector open until the full command timeout even though
                            # the recorded process has already exited.
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        if now - process_exited_at >= 2:
                            # A descriptor inherited outside the process group must not
                            # prevent the main command's result from being recorded.
                            for key in tuple(selector.get_map().values()):
                                selector.unregister(key.fileobj)
                            break
                    remaining = deadline - now
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
            "schema_version": 2,
            "return_code": return_code,
            "stdout_bytes": stdout_path.stat().st_size,
            "stderr_bytes": stderr_path.stat().st_size,
            "elapsed_seconds": time.monotonic() - monotonic_started,
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
    legacy = {"schema_version", "return_code", "stdout_bytes", "stderr_bytes"}
    current = legacy | {"elapsed_seconds"}
    if (
        not isinstance(value, dict)
        or set(value) not in (legacy, current)
        or value["schema_version"] not in (1, 2)
        or (value["schema_version"] == 1 and set(value) != legacy)
        or (value["schema_version"] == 2 and set(value) != current)
        or any(
            isinstance(value[field], bool) or not isinstance(value[field], int)
            for field in ("return_code", "stdout_bytes", "stderr_bytes")
        )
    ):
        raise StateError("process has no valid result")
    if value["schema_version"] == 2 and (
        isinstance(value["elapsed_seconds"], bool)
        or not isinstance(value["elapsed_seconds"], (int, float))
        or value["elapsed_seconds"] < 0
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
