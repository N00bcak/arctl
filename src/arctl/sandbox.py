"""Codex permission-profile command construction."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Sequence

from .errors import StateError

_MARK_AND_EXEC = """\
import os
import sys
from pathlib import Path

Path(sys.argv[1]).write_text("started")
os.execvp(sys.argv[2], sys.argv[2:])
"""


def marked_command(command: Sequence[str], marker: Path) -> tuple[str, ...]:
    if not command:
        raise ValueError("marked command must not be empty")
    return (
        str(Path(sys.executable).resolve()),
        "-c",
        _MARK_AND_EXEC,
        str(marker.resolve()),
        *command,
    )


def command_runtime_read_paths(command: Sequence[str]) -> tuple[Path, ...]:
    if not command:
        raise ValueError("command must not be empty")
    executable = Path(command[0])
    if not executable.is_absolute():
        located = shutil.which(command[0])
        if located is None:
            return ()
        executable = Path(located)
    resolved = executable.resolve()
    paths = [resolved]
    launcher_root = executable.parent.parent
    if (
        executable.parent.name == "shims"
        and (launcher_root / "libexec" / "pyenv").is_file()
    ):
        paths.insert(0, launcher_root.resolve())
    environment = executable.parent.parent
    if executable.parent.name in {"bin", "Scripts"} and (
        environment / "pyvenv.cfg"
    ).is_file():
        paths.insert(0, environment.resolve())
    runtime = resolved.parent.parent
    runtime_library = runtime / "lib"
    if runtime_library.is_dir() and any(runtime_library.glob("libpython*")):
        paths.append(runtime.resolve())
    return tuple(dict.fromkeys(paths))


def _filesystem_override(
    profile: str,
    *,
    read_paths: Sequence[Path],
    write_paths: Sequence[Path],
    codex: str,
) -> str:
    entries: dict[str, str] = {":root": "deny", ":minimal": "read"}
    for path in read_paths:
        entries[str(path.resolve())] = "read"
    executable = shutil.which(codex)
    if executable is None:
        raise StateError(f"Codex executable is not available: {codex}")
    resolved_executable = Path(executable).resolve()
    runtime_path = (
        resolved_executable.parent.parent
        if resolved_executable.suffix == ".js" and resolved_executable.parent.name == "bin"
        else resolved_executable
    )
    entries[str(runtime_path)] = "read"
    for path in write_paths:
        resolved = str(path.resolve())
        if resolved in entries:
            raise StateError(f"sandbox path cannot be both read-only and writable: {resolved}")
        entries[resolved] = "write"
    table = ",".join(
        f"{json.dumps(path)}={json.dumps(access)}"
        for path, access in sorted(entries.items())
    )
    return f"permissions.{profile}.filesystem={{{table}}}"


def sandbox_command(
    command: Sequence[str],
    *,
    cwd: Path,
    read_paths: Sequence[Path],
    write_paths: Sequence[Path],
    profile: str,
    codex: str = "codex",
) -> tuple[str, ...]:
    if not command:
        raise ValueError("sandboxed command must not be empty")
    return (
        codex,
        "sandbox",
        "--cd",
        str(cwd.resolve()),
        "--permission-profile",
        profile,
        "--config",
        _filesystem_override(
            profile,
            read_paths=(*read_paths, *command_runtime_read_paths(command)),
            write_paths=write_paths,
            codex=codex,
        ),
        "--config",
        f"permissions.{profile}.network.enabled=false",
        "--",
        *command,
    )


def research_command(
    *,
    worktree: Path,
    scratch: Path,
    output_schema: Path,
    prompt: str,
    codex: str = "codex",
) -> tuple[str, ...]:
    profile = "arctl-research"
    return (
        codex,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--json",
        "--cd",
        str(worktree.resolve()),
        "--output-schema",
        str(output_schema.resolve()),
        "--output-last-message",
        str((scratch / "request.public.json").resolve()),
        "--config",
        f"default_permissions={json.dumps(profile)}",
        "--config",
        "approval_policy=\"never\"",
        "--config",
        "web_search=\"disabled\"",
        "--config",
        _filesystem_override(
            profile,
            read_paths=(worktree / ".git",),
            write_paths=(worktree, scratch),
            codex=codex,
        ),
        "--config",
        f"permissions.{profile}.network.enabled=false",
        "--disable",
        "multi_agent",
        "--disable",
        "apps",
        "--disable",
        "browser_use",
        "--disable",
        "computer_use",
        "--disable",
        "plugins",
        "--disable",
        "image_generation",
        prompt,
    )


def sanitized_environment(
    *,
    codex_home: Path,
    writable_home: Path,
) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(writable_home.resolve()),
        "CODEX_HOME": str(codex_home.resolve()),
        "TMPDIR": str(writable_home.resolve()),
    }
    for name in ("LANG", "LC_ALL", "TZ"):
        if value := os.environ.get(name):
            environment[name] = value
    return environment
