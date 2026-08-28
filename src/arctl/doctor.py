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
import time
from pathlib import Path
from typing import Any

from .errors import PreflightError, ProcessError, StateError
from .platform_process import inspect_process, process_backend
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
) -> tuple[bool, str | None]:
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
        detail = (completed.stderr or completed.stdout).strip()
        if "sandbox_apply: Operation not permitted" in detail:
            return (
                False,
                "macOS Seatbelt could not be nested; run arctl from a normal "
                "unsandboxed Terminal or CI process",
            )
        return False, detail.splitlines()[0] if detail else "sandbox command did not run"
    try:
        checks = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "sandbox probe did not produce valid JSON"
    passed = isinstance(checks, dict) and bool(checks) and all(checks.values())
    return (
        (True, None)
        if passed
        else (False, "sandbox profile did not enforce every required boundary")
    )


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
            # Leave enough time for the gated runner and nested interpreter to
            # start and publish child.pid before exercising timeout cleanup.
            # A shorter window made this probe report a cleanup failure when
            # the descendant had never actually been created on a busy host.
            timeout_seconds=1.0,
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
    # Group SIGKILL delivery and orphan/zombie bookkeeping are asynchronous on
    # macOS.  The managed runner has already waited for the group leader, but a
    # descendant can remain observable as running for a few scheduler ticks.
    # Give the kernel a short bounded convergence window before failing doctor.
    deadline = time.monotonic() + 1.0
    while True:
        identity = inspect_process(child)
        if identity is None or not identity.alive:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def doctor_succeeded(report: dict[str, Any]) -> bool:
    checks = report.get("checks")
    return isinstance(checks, dict) and bool(checks) and all(checks.values())


def require_doctor() -> dict[str, Any]:
    report = run_doctor()
    if doctor_succeeded(report):
        return report
    failed = [name for name, passed in report["checks"].items() if not passed]
    raise PreflightError(
        "runtime or sandbox preflight failed: " + ", ".join(failed),
        report,
    )


def run_doctor() -> dict[str, Any]:
    system = platform.system()
    supported = system in {"Linux", "Darwin"}
    backend: str | None = None
    process_available = False
    diagnostics: dict[str, str] = {}
    if supported:
        try:
            backend = process_backend(system)
            identity = inspect_process(os.getpid())
            process_available = (
                identity is not None
                and identity.alive
                and identity.platform == system
            )
        except (ProcessError, StateError) as error:
            diagnostics["process_backend"] = str(error)
    else:
        diagnostics["supported_platform"] = (
            f"unsupported operating system {system or 'unknown'}; "
            "arctl supports Linux and macOS"
        )

    sandbox_backend = (
        "bubblewrap" if system == "Linux" else "seatbelt" if system == "Darwin" else None
    )
    sandbox_available = (
        shutil.which("bwrap") is not None
        if system == "Linux"
        else Path("/usr/bin/sandbox-exec").is_file()
        if system == "Darwin"
        else False
    )
    checks: dict[str, bool] = {
        "supported_platform": supported,
        "process_backend": process_available,
        "sandbox_backend": sandbox_available,
        "python_3_11": sys.version_info >= (3, 11),
        "git": shutil.which("git") is not None,
        "codex": shutil.which("codex") is not None,
        "uv": shutil.which("uv") is not None,
        "pyyaml": importlib.util.find_spec("yaml") is not None,
        "jsonschema": importlib.util.find_spec("jsonschema") is not None,
        "research_profile": False,
        "subject_profile": False,
        "evaluator_profile": False,
        "timeout_child_cleanup": False,
    }
    if not process_available and "process_backend" not in diagnostics:
        diagnostics["process_backend"] = "managed process identity probe failed"
    if not sandbox_available:
        diagnostics["sandbox_backend"] = (
            "Bubblewrap is required for Linux online dependency provisioning"
            if system == "Linux"
            else "the built-in macOS Seatbelt executable is unavailable"
            if system == "Darwin"
            else "no supported sandbox backend is available"
        )
    prerequisite_names = (
        "supported_platform",
        "process_backend",
        "sandbox_backend",
        "python_3_11",
        "git",
        "codex",
        "uv",
        "pyyaml",
        "jsonschema",
    )
    for name in prerequisite_names:
        if not checks[name] and name not in diagnostics:
            diagnostics[name] = f"required prerequisite failed: {name}"
    if not all(checks[name] for name in prerequisite_names):
        for name in (
            "research_profile",
            "subject_profile",
            "evaluator_profile",
            "timeout_child_cleanup",
        ):
            diagnostics[name] = "not run because a required prerequisite failed"
        return {
            "schema_version": 2,
            "runtime": {
                "system": system,
                "architecture": platform.machine(),
                "process_backend": backend,
                "sandbox_backend": sandbox_backend,
            },
            "checks": checks,
            "diagnostics": diagnostics,
        }

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
        secret = private / "calibration.private.json"
        secret.write_text("private")
        (target / "hidden").write_text("target")
        research_scratch = root / "research-output"
        subject_output = root / "subject-output"
        evaluator_output = root / "evaluator-output"
        for directory in (research_scratch, subject_output, evaluator_output):
            directory.mkdir()
        profiles = (
            (
                "research_profile",
                {
                    "profile": "arctl-research",
                    "cwd": research,
                    "allowed_read": allowed[research],
                    "denied_read": secret,
                    "allowed_write": research_scratch,
                    "denied_write": private,
                    "read_paths": (),
                    "write_paths": (research, research_scratch),
                },
            ),
            (
                "subject_profile",
                {
                    "profile": "arctl-subject",
                    "cwd": subject,
                    "allowed_read": allowed[subject],
                    "denied_read": secret,
                    "allowed_write": subject_output,
                    "denied_write": subject,
                    "read_paths": (subject,),
                    "write_paths": (subject_output,),
                },
            ),
            (
                "evaluator_profile",
                {
                    "profile": "arctl-evaluator",
                    "cwd": evaluator,
                    "allowed_read": allowed[evaluator],
                    "denied_read": target / "hidden",
                    "allowed_write": evaluator_output,
                    "denied_write": target,
                    "read_paths": (evaluator, private),
                    "write_paths": (evaluator_output,),
                },
            ),
        )
        for name, arguments in profiles:
            try:
                passed, diagnostic = _profile_probe(root, **arguments)
            except (OSError, StateError, subprocess.SubprocessError) as error:
                passed, diagnostic = False, str(error)
            checks[name] = passed
            if diagnostic is not None:
                diagnostics[name] = diagnostic
        try:
            checks["timeout_child_cleanup"] = _process_cleanup_probe(root)
        except (OSError, ProcessError, StateError, subprocess.SubprocessError) as error:
            diagnostics["timeout_child_cleanup"] = str(error)
        if not checks["timeout_child_cleanup"]:
            diagnostics.setdefault(
                "timeout_child_cleanup",
                "managed process timeout did not clean up its descendant",
            )
    return {
        "schema_version": 2,
        "runtime": {
            "system": system,
            "architecture": platform.machine(),
            "process_backend": backend,
            "sandbox_backend": sandbox_backend,
        },
        "checks": checks,
        "diagnostics": diagnostics,
    }
