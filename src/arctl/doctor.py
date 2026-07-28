"""Runtime and sandbox preflight checks."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .errors import ProcessError, StateError
from .process import run_once
from .sandbox import (
    command_runtime_read_paths,
    marked_command,
    sandbox_command,
    sanitized_environment,
)

_PROBE = """\
import json
import socket
import sys
from pathlib import Path

allowed_read, denied_read, allowed_write, denied_write, result = map(Path, sys.argv[1:])
checks = {"read": allowed_read.read_text() == "allowed"}
try:
    denied_read.read_bytes()
except OSError:
    checks["read_denial"] = True
else:
    checks["read_denial"] = False
try:
    (denied_write / "forbidden").write_text("bad")
except OSError:
    checks["write_denial"] = True
else:
    checks["write_denial"] = False
try:
    socket.create_connection(("1.1.1.1", 53), timeout=0.5)
except OSError:
    checks["network_denial"] = True
else:
    checks["network_denial"] = False
(allowed_write / "probe.json").write_text(json.dumps(checks))
"""


def _profile_probe(
    root: Path,
    *,
    profile: str,
    cwd: Path,
    allowed_read: Path,
    denied_read: Path,
    allowed_write: Path,
    denied_write: Path,
    read_paths: tuple[Path, ...],
    write_paths: tuple[Path, ...],
) -> bool:
    result = allowed_write / "probe.json"
    marker = allowed_write / "execution.started"
    probe = (
        sys.executable,
        "-c",
        _PROBE,
        str(allowed_read),
        str(denied_read),
        str(allowed_write),
        str(denied_write),
        str(result),
    )
    command = sandbox_command(
        marked_command(probe, marker),
        cwd=cwd,
        read_paths=(*read_paths, *command_runtime_read_paths(probe)),
        write_paths=write_paths,
        profile=profile,
    )
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=sanitized_environment(
            codex_home=root / "codex-home",
            writable_home=allowed_write,
        ),
        timeout=10,
    )
    if completed.returncode or not marker.is_file() or not result.is_file():
        return False
    try:
        checks = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(checks, dict) and bool(checks) and all(checks.values())


def _process_cleanup_probe(root: Path) -> bool:
    pid_file = root / "child.pid"
    script = (
        "import pathlib,subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid));"
        "time.sleep(30)"
    )
    try:
        run_once(
            root / "timeout-process",
            ("python3", "-c", script),
            timeout_seconds=0.3,
            max_output_bytes=1000,
        )
    except ProcessError:
        pass
    else:
        return False
    try:
        child = int(pid_file.read_text())
    except (OSError, ValueError):
        return False
    try:
        os.kill(child, 0)
    except ProcessLookupError:
        return True
    stat = Path(f"/proc/{child}/stat")
    return stat.is_file() and stat.read_text().split()[2] == "Z"


def run_doctor() -> dict[str, Any]:
    checks: dict[str, bool] = {
        "linux": platform.system() == "Linux",
        "python_3_11": sys.version_info >= (3, 11),
        "git": shutil.which("git") is not None,
        "codex": shutil.which("codex") is not None,
        "pyyaml": importlib.util.find_spec("yaml") is not None,
        "jsonschema": importlib.util.find_spec("jsonschema") is not None,
    }
    if not all(checks.values()):
        return checks

    with tempfile.TemporaryDirectory(prefix="arctl-doctor-") as temporary:
        root = Path(temporary)
        research = root / "research"
        subject = root / "subject"
        evaluator = root / "evaluator"
        private = root / "private"
        target = root / "target"
        codex_home = root / "codex-home"
        for directory in (
            research,
            subject,
            evaluator,
            private,
            target,
            codex_home,
        ):
            directory.mkdir()
        allowed = {
            research: research / "allowed",
            subject: subject / "allowed",
            evaluator: evaluator / "allowed",
        }
        for path in allowed.values():
            path.write_text("allowed")
        secret = private / "secret"
        secret.write_text("private")
        (target / "hidden").write_text("target")
        research_scratch = root / "research-output"
        subject_output = root / "subject-output"
        evaluator_output = root / "evaluator-output"
        for directory in (research_scratch, subject_output, evaluator_output):
            directory.mkdir()
        try:
            checks["research_profile"] = _profile_probe(
                root,
                profile="arctl-research",
                cwd=research,
                allowed_read=allowed[research],
                denied_read=secret,
                allowed_write=research_scratch,
                denied_write=private,
                read_paths=(),
                write_paths=(research, research_scratch),
            )
            checks["subject_profile"] = _profile_probe(
                root,
                profile="arctl-subject",
                cwd=subject,
                allowed_read=allowed[subject],
                denied_read=secret,
                allowed_write=subject_output,
                denied_write=subject,
                read_paths=(subject,),
                write_paths=(subject_output,),
            )
            checks["evaluator_profile"] = _profile_probe(
                root,
                profile="arctl-evaluator",
                cwd=evaluator,
                allowed_read=allowed[evaluator],
                denied_read=target / "hidden",
                allowed_write=evaluator_output,
                denied_write=target,
                read_paths=(evaluator, private),
                write_paths=(evaluator_output,),
            )
        except (OSError, StateError, subprocess.SubprocessError):
            checks["research_profile"] = False
            checks["subject_profile"] = False
            checks["evaluator_profile"] = False
        checks["timeout_child_cleanup"] = _process_cleanup_probe(root)
    return checks
