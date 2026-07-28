"""Validation and rendering for approved argument-vector commands."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Sequence

from .errors import ValidationError

_BLOCKED_PROGRAMS = frozenset(
    {"bash", "dash", "env", "fish", "sh", "sudo", "zsh"}
)


def validate_command_template(
    value: object,
    *,
    label: str,
    placeholders: Sequence[str],
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{label} must be a non-empty argument-vector command")
    if any(not isinstance(argument, str) or not argument for argument in value):
        raise ValidationError(f"{label} arguments must be non-empty strings")
    command = tuple(value)
    if Path(command[0]).name in _BLOCKED_PROGRAMS:
        raise ValidationError(f"{label} uses blocked program: {command[0]}")

    allowed = {f"{{{placeholder}}}" for placeholder in placeholders}
    seen: list[str] = []
    for argument in command:
        if "{" in argument or "}" in argument:
            if argument not in allowed:
                raise ValidationError(f"{label} contains an unknown or partial placeholder")
            seen.append(argument)
    expected = sorted(allowed)
    if sorted(seen) != expected:
        raise ValidationError(f"{label} must contain each approved placeholder exactly once")
    return command


def render_command(
    template: Sequence[str],
    substitutions: Mapping[str, Path],
    *,
    allowed_roots: Sequence[Path],
) -> tuple[str, ...]:
    rendered_paths: dict[str, str] = {}
    roots = tuple(root.resolve() for root in allowed_roots)
    for name, path in substitutions.items():
        if not path.is_absolute():
            raise ValidationError(f"{name} path must be absolute")
        resolved = path.resolve(strict=False)
        if not any(os.path.commonpath((resolved, root)) == str(root) for root in roots):
            raise ValidationError(f"{name} path escapes its allowed roots")
        rendered_paths[f"{{{name}}}"] = str(resolved)

    rendered: list[str] = []
    for argument in template:
        if "{" in argument or "}" in argument:
            try:
                argument = rendered_paths[argument]
            except KeyError as error:
                raise ValidationError("command contains an unresolved placeholder") from error
        rendered.append(argument)
    return tuple(rendered)
