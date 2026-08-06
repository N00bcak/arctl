"""Persistent component-local agent selection."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import StateError
from .methods import AgentDefinition, MethodConfig
from .storage import write_json_once

Chooser = Callable[[tuple[str, ...]], str]


def select_agent(
    method: MethodConfig,
    *,
    component: str,
    lifecycle: str,
    root: Path,
    chooser: Chooser | None = None,
) -> AgentDefinition:
    pool = tuple(agent.name for agent in method.pool(component))
    if not pool:
        raise StateError(f"component {component} has no agent pool")
    policy = (
        "uniform-with-replacement-v1"
        if method.profile == "serial-hotseat-v1"
        else "single-v1"
    )
    path = root / "agent-selection.public.json"
    if path.is_file():
        try:
            value: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("saved agent selection is invalid") from error
        expected = {
            "schema_version": 1,
            "component": component,
            "lifecycle": lifecycle,
            "selection_policy": policy,
            "agent_pool": list(pool),
        }
        if not isinstance(value, dict) or {
            key: value.get(key) for key in expected
        } != expected or value.get("selected_agent") not in pool or set(value) != {
            *expected,
            "selected_agent",
        }:
            raise StateError("saved agent selection differs from the approved lifecycle")
        return method.agents[value["selected_agent"]]
    selected = pool[0] if policy == "single-v1" else (chooser or secrets.choice)(pool)
    if selected not in pool:
        raise StateError("agent chooser selected outside the component pool")
    write_json_once(
        path,
        {
            "schema_version": 1,
            "component": component,
            "lifecycle": lifecycle,
            "selection_policy": policy,
            "agent_pool": list(pool),
            "selected_agent": selected,
        },
    )
    return method.agents[selected]
