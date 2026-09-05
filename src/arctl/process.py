"""Exactly-once managed process execution."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import platform
import re
import shutil
import signal
import selectors
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Sequence

from .errors import ProcessError, StateError, StoppedError
from .platform_process import boot_identity, inspect_process
from .storage import atomic_write_json

_GATED_EXEC = """\
import os
import sys

descriptor = int(sys.argv[1])
token_descriptor = int(sys.argv[2])
with os.fdopen(descriptor, "rb", closefd=True) as gate:
    released = gate.read(1)
if released != b"1":
    raise SystemExit(125)
os.set_inheritable(token_descriptor, True)
os.execvp(sys.argv[3], sys.argv[3:])
"""


def _load_process_record(directory: Path) -> dict[str, Any] | None:
    path = directory / "process.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("managed process identity is invalid") from error
    fields = {
        "pid", "pgid", "platform", "start_time", "boot_identity",
        "launch_token", "launch_token_file", "launch_token_identity",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise StateError("managed process identity is invalid")
    integer_fields = ("pid", "pgid", "start_time")
    if any(
        isinstance(value[field], bool)
        or not isinstance(value[field], int)
        or value[field] <= 0
        for field in integer_fields
    ):
        raise StateError("managed process identity is invalid")
    if value["platform"] not in {"Linux", "Darwin"}:
        raise StateError("managed process identity is invalid")
    if any(
        not isinstance(value[field], str) or not value[field]
        for field in ("boot_identity", "launch_token", "launch_token_file")
    ):
        raise StateError("managed process identity is invalid")
    token_path = Path(value["launch_token_file"])
    if token_path.is_absolute() or token_path.parts != ("launch.token",):
        raise StateError("managed process identity is invalid")
    token_identity = value["launch_token_identity"]
    if (
        not isinstance(token_identity, dict)
        or set(token_identity) != {"device", "inode"}
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in token_identity.values()
        )
    ):
        raise StateError("managed process identity is invalid")
    return value


def _matching_managed_process(
    directory: Path, value: Mapping[str, Any]
) -> Any | None:
    token = directory / value["launch_token_file"]
    if token.is_symlink() or not token.is_file():
        raise StateError("managed process launch token is invalid")
    token_stat = token.stat(follow_symlinks=False)
    if {"device": token_stat.st_dev, "inode": token_stat.st_ino} != value[
        "launch_token_identity"
    ]:
        raise StateError("managed process launch token identity changed")
    if token.read_text(encoding="utf-8") != value["launch_token"]:
        raise StateError("managed process launch token is invalid")
    if value["boot_identity"] != boot_identity():
        return None
    with token.open("r+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            return None
        finally:
            try:
                fcntl.flock(stream, fcntl.LOCK_UN)
            except OSError:
                pass
    current = inspect_process(value["pid"])
    if current is None or not current.alive:
        return None
    recorded_pgid = value["pgid"]
    if current.platform != value["platform"]:
        return None
    if current.start_time != value["start_time"] or current.pgid != recorded_pgid:
        return None
    return current


def recorded_process_is_live(directory: Path) -> bool:
    """Return liveness using the complete managed-process identity predicate."""
    value = _load_process_record(directory)
    return value is not None and _matching_managed_process(directory, value) is not None


def _kill_recorded_process(directory: Path) -> None:
    value = _load_process_record(directory)
    if value is None:
        return
    current = _matching_managed_process(directory, value)
    if current is None:
        return
    pid = value["pid"]
    recorded_pgid = value["pgid"]
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
    token_stream: Any = None
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
            token_path = directory / "launch.token"
            launch_token = uuid.uuid4().hex
            descriptor = os.open(
                token_path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            token_stream = os.fdopen(descriptor, "r+")
            token_stream.write(launch_token)
            token_stream.flush()
            os.fsync(token_stream.fileno())
            fcntl.flock(token_stream, fcntl.LOCK_EX)
            token_stat = os.fstat(token_stream.fileno())
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _GATED_EXEC,
                    str(gate_read),
                    str(token_stream.fileno()),
                    *command,
                ],
                cwd=process_directory,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=env,
                pass_fds=(gate_read, token_stream.fileno()),
            )
            os.close(gate_read)
            gate_read = None
            identity = inspect_process(process.pid)
            if identity is None or not identity.alive:
                raise ProcessError("could not identify managed process")
            atomic_write_json(
                directory / "process.json",
                {
                    "platform": identity.platform,
                    "pid": identity.pid,
                    "pgid": identity.pgid,
                    "start_time": identity.start_time,
                    "boot_identity": boot_identity(),
                    "launch_token": launch_token,
                    "launch_token_file": "launch.token",
                    "launch_token_identity": {
                        "device": token_stat.st_dev,
                        "inode": token_stat.st_ino,
                    },
                },
            )
            token_stream.close()
            token_stream = None
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
        if token_stream is not None:
            token_stream.close()
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
    current = {"return_code", "stdout_bytes", "stderr_bytes", "elapsed_seconds"}
    if (
        not isinstance(value, dict)
        or set(value) != current
        or any(
            isinstance(value[field], bool) or not isinstance(value[field], int)
            for field in ("return_code", "stdout_bytes", "stderr_bytes")
        )
    ):
        raise StateError("process has no valid result")
    if (
        isinstance(value["elapsed_seconds"], bool)
        or not isinstance(value["elapsed_seconds"], (int, float))
        or value["elapsed_seconds"] < 0
    ):
        raise StateError("process has no valid result")
    return value


def read_valid_started(directory: Path) -> dict[str, Any]:
    path = directory / "started.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("process has no valid started record") from error
    base = {"command", "cwd", "environment", "stop_path"}
    fields = (base, base | {"stdin"})
    if (
        not isinstance(value, dict)
        or set(value) not in fields
        or not isinstance(value["command"], list)
        or not value["command"]
        or not all(isinstance(argument, str) for argument in value["command"])
    ):
        raise StateError("process has no valid started record")
    for field in ("cwd", "stop_path"):
        raw = value[field]
        if raw is not None and (
            not isinstance(raw, str) or not raw or not Path(raw).is_absolute()
        ):
            raise StateError("process has no valid started record")
    environment = value["environment"]
    if environment is not None and (
        not isinstance(environment, dict)
        or not all(
            isinstance(name, str)
            and name
            and isinstance(raw, str)
            for name, raw in environment.items()
        )
    ):
        raise StateError("process has no valid started record")
    if "stdin" in value:
        stdin = value["stdin"]
        if (
            not isinstance(stdin, dict)
            or set(stdin) != {"path", "bytes", "sha256"}
            or not isinstance(stdin["path"], str)
            or not Path(stdin["path"]).is_absolute()
            or isinstance(stdin["bytes"], bool)
            or not isinstance(stdin["bytes"], int)
            or stdin["bytes"] < 0
            or not isinstance(stdin["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", stdin["sha256"])
        ):
            raise StateError("process has no valid started record")
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
        "command": list(command),
        "cwd": str(cwd.resolve()),
        "environment": dict(env) if env is not None else None,
        "stop_path": str(stop_path.resolve()) if stop_path is not None else None,
        **({"stdin": stdin_record} if stdin_record is not None else {}),
    }
    started_path = directory / "started.json"
    if (directory / "result.json").exists():
        started = read_valid_started(directory)
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
