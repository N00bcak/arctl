"""Codex permission-profile command construction."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Sequence

from .codex_schema import load_codex_output_schema
from .errors import StateError
from .storage import atomic_write_text

MAX_AGENT_PROMPT_BYTES = 32 * 1024

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
    network_enabled: bool = False,
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
        f"permissions.{profile}.network.enabled={str(network_enabled).lower()}",
        "--",
        *command,
    )


def networked_dependency_command(
    command: Sequence[str],
    *,
    cwd: Path,
    read_paths: Sequence[Path] = (),
    write_paths: Sequence[Path],
) -> tuple[str, ...]:
    """Confine an online package installer without hiding host DNS/networking."""
    if not command:
        raise ValueError("networked dependency command must not be empty")
    system = platform.system()
    if system not in {"Linux", "Darwin"}:
        raise StateError(
            f"unsupported operating system {system or 'unknown'}; "
            "arctl supports Linux and macOS"
        )
    for path in write_paths:
        path.mkdir(parents=True, exist_ok=True)
    if system == "Darwin":
        return sandbox_command(
            command,
            cwd=cwd,
            read_paths=read_paths,
            write_paths=write_paths,
            profile="arctl-setup-dependencies",
            network_enabled=True,
        )
    bubblewrap = shutil.which("bwrap")
    if bubblewrap is None:
        raise StateError("bwrap is required for networked dependency provisioning")
    arguments = [
        bubblewrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/arctl-tools",
    ]
    executable = Path(command[0])
    if not executable.is_absolute():
        located = shutil.which(command[0])
        if located is None:
            raise StateError(f"dependency executable is unavailable: {command[0]}")
        executable = Path(located)
    executable = executable.resolve()
    sandbox_executable = Path("/arctl-tools") / executable.name
    arguments.extend(
        ("--ro-bind", str(executable.parent), "/arctl-tools")
    )
    system_paths = (
        Path("/usr"),
        Path("/bin"),
        Path("/lib"),
        Path("/lib64"),
        Path("/etc/resolv.conf"),
        Path("/etc/hosts"),
        Path("/etc/nsswitch.conf"),
        Path("/etc/gai.conf"),
        Path("/etc/ssl"),
        Path("/etc/ca-certificates"),
    )
    for path in system_paths:
        if path.exists():
            arguments.extend(("--ro-bind", str(path), str(path)))
    mounted_roots: list[Path] = [path.resolve() for path in system_paths if path.is_dir()]
    for path in (*read_paths, *command_runtime_read_paths(command)):
        resolved = path.resolve()
        if resolved == executable or not resolved.exists() or any(
            resolved == root or root in resolved.parents for root in mounted_roots
        ):
            continue
        arguments.extend(("--ro-bind", str(resolved), str(resolved)))
        mounted_roots.append(resolved)
    for path in write_paths:
        resolved = path.resolve()
        arguments.extend(("--bind", str(resolved), str(resolved)))
    arguments.extend(
        (
            "--chdir",
            str(cwd.resolve()),
            "--",
            str(sandbox_executable),
            *command[1:],
        )
    )
    return tuple(arguments)


def research_command(
    *,
    worktree: Path,
    scratch: Path,
    output_schema: Path,
    prompt: str,
    read_paths: Sequence[Path] = (),
    codex: str = "codex",
    output_name: str = "request.public.json",
    model: str | None = None,
    reasoning_effort: str | None = None,
    writable_worktree: bool = True,
    read_worktree: bool = True,
    network_enabled: bool = False,
) -> tuple[str, ...]:
    encoded_prompt = prompt.encode("utf-8")
    if len(encoded_prompt) > MAX_AGENT_PROMPT_BYTES:
        raise StateError(
            "agent prompt exceeds the global limit: "
            f"{len(encoded_prompt)} > {MAX_AGENT_PROMPT_BYTES} bytes"
        )
    load_codex_output_schema(output_schema)
    prompt_path = scratch.parent / "prompt.public.txt"
    if prompt_path.is_symlink():
        raise StateError("saved agent prompt must not be a symlink")
    if prompt_path.is_file():
        if prompt_path.read_bytes() != encoded_prompt:
            raise StateError("saved agent prompt differs from the requested prompt")
    elif prompt_path.exists():
        raise StateError("saved agent prompt is not a regular file")
    else:
        atomic_write_text(prompt_path, prompt)
    profile = "arctl-research"
    overrides: tuple[str, ...] = ()
    if reasoning_effort is not None:
        overrides += ("--config", f"model_reasoning_effort={json.dumps(reasoning_effort)}")
    model_option: tuple[str, ...] = ("--model", model) if model is not None else ()
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
        str((scratch / output_name).resolve()),
        *model_option,
        *overrides,
        "--config",
        f"default_permissions={json.dumps(profile)}",
        "--config",
        "approval_policy=\"never\"",
        "--config",
        f"web_search={json.dumps('live' if network_enabled else 'disabled')}",
        "--config",
        _filesystem_override(
            profile,
            read_paths=(
                (worktree / ".git", *read_paths)
                if writable_worktree
                else ((worktree, *read_paths) if read_worktree else tuple(read_paths))
            ),
            write_paths=((worktree, scratch) if writable_worktree else (scratch,)),
            codex=codex,
        ),
        "--config",
        f"permissions.{profile}.network.enabled={str(network_enabled).lower()}",
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
        "-",
    )


def agent_prompt_path(scratch: Path) -> Path:
    return scratch.parent / "prompt.public.txt"


def sanitized_environment(
    *,
    codex_home: Path,
    writable_home: Path,
) -> dict[str, str]:
    resolved_home = writable_home.resolve()
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(resolved_home),
        "CODEX_HOME": str(codex_home.resolve()),
        "TMPDIR": str(resolved_home),
        "PYTHONPYCACHEPREFIX": str(resolved_home / "pycache"),
    }
    for name in ("LANG", "LC_ALL", "TZ"):
        if value := os.environ.get(name):
            environment[name] = value
    return environment
