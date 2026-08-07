"""Installed trusted research-component registry and dispatcher."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable

from .errors import ValidationError


@dataclass(frozen=True)
class ComponentSpec:
    stage: str
    identifier: str
    contract_version: int
    handler: str
    agent_driven: bool


_SPECS = (
    ComponentSpec(
        "search",
        "search.serial-champion-v1",
        1,
        "arctl.runner:_candidate_search",
        False,
    ),
    ComponentSpec(
        "strategize",
        "strategize.environment-v1",
        1,
        "arctl.search:ensure_strategy",
        True,
    ),
    ComponentSpec(
        "plan", "plan.comparative-v1", 1, "arctl.runner:_run_planning", True
    ),
    ComponentSpec(
        "execute",
        "execute.worktree-v1",
        1,
        "arctl.runner:_run_implementation",
        True,
    ),
    ComponentSpec(
        "evaluate",
        "evaluate.paired-suspect-v1",
        1,
        "arctl.runner:_run_reserved_comparison",
        False,
    ),
    ComponentSpec(
        "reflect",
        "reflect.evidence-v1",
        1,
        "arctl.reflection:run_reflection",
        True,
    ),
)
INSTALLED_COMPONENTS = MappingProxyType({spec.identifier: spec for spec in _SPECS})


def resolve_component(stage: str, identifier: str) -> ComponentSpec:
    try:
        spec = INSTALLED_COMPONENTS[identifier]
    except KeyError as error:
        raise ValidationError(f"component {identifier} is not installed") from error
    if spec.stage != stage:
        raise ValidationError(f"component {identifier} is incompatible with {stage}")
    return spec


def invoke_component(stage: str, identifier: str, *args: Any, **kwargs: Any) -> Any:
    spec = resolve_component(stage, identifier)
    module_name, function_name = spec.handler.split(":", 1)
    handler: Callable[..., Any] = getattr(
        importlib.import_module(module_name), function_name
    )
    return handler(*args, **kwargs)
