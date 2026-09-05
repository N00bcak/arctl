"""Research-method configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .components import resolve_component
from .errors import ValidationError

ReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh"]
_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}
_AGENT_COMPONENTS = {"strategize", "plan", "execute", "reflect"}
_COMPONENT_NAMES = {"search", "strategize", "plan", "execute", "evaluate", "reflect"}


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    backend: str
    model: str
    reasoning_effort: ReasoningEffort


@dataclass(frozen=True)
class ComponentDefinition:
    identifier: str
    agent_pool: tuple[str, ...]


@dataclass(frozen=True)
class MethodConfig:
    profile: Literal["serial", "serial-hotseat"]
    allow_unverified_isolation: bool
    agents: Mapping[str, AgentDefinition]
    components: Mapping[str, ComponentDefinition]

    def pool(self, component: str) -> tuple[AgentDefinition, ...]:
        try:
            return tuple(
                self.agents[name] for name in self.components[component].agent_pool
            )
        except KeyError as error:
            raise ValidationError(f"method has no {component} agent pool") from error

    def require_component(self, component: str, implementation: str) -> None:
        try:
            selected = self.components[component].identifier
        except KeyError as error:
            raise ValidationError(f"method has no {component} component") from error
        resolve_component(component, selected)
        if selected != implementation:
            raise ValidationError(
                f"component {component} requires an installed implementation: {selected}"
            )

    def to_lock(self) -> dict[str, Any]:
        """Return semantic configuration; runtime certification is separate."""
        return {
            "profile": self.profile,
            "selection_policy": (
                "uniform-with-replacement"
                if self.profile == "serial-hotseat"
                else "single"
            ),
            "allow_unverified_isolation": self.allow_unverified_isolation,
            "agents": {
                name: {
                    "backend": agent.backend,
                    "model": agent.model,
                    "settings": {"reasoning_effort": agent.reasoning_effort},
                }
                for name, agent in sorted(self.agents.items())
            },
            "components": {
                name: {
                    "component": component.identifier,
                    "agent_pool": list(component.agent_pool),
                }
                for name, component in sorted(self.components.items())
            },
        }


def _defaults(
    profile: str,
) -> tuple[dict[str, AgentDefinition], dict[str, ComponentDefinition]]:
    base: dict[str, tuple[str, ReasoningEffort]] = {
        "strategy": ("gpt-5.6-sol", "medium"),
        "planning": ("gpt-5.6-sol", "medium"),
        "execution": ("gpt-5.6-terra", "medium"),
        "reflection": ("gpt-5.6-sol", "medium"),
    }
    agents: dict[str, AgentDefinition] = {}
    pools: dict[str, tuple[str, ...]] = {}
    component_agent = {
        "strategize": "strategy",
        "plan": "planning",
        "execute": "execution",
        "reflect": "reflection",
    }
    for component, stem in component_agent.items():
        names = (f"{stem}-default",)
        if profile == "serial-hotseat":
            names = (f"{stem}-a", f"{stem}-b")
        model, effort = base[stem]
        for name in names:
            agents[name] = AgentDefinition(name, "codex-cli", model, effort)
        pools[component] = names
    components = {
        "search": ComponentDefinition("search.serial-champion", ()),
        "strategize": ComponentDefinition(
            "strategize.environment", pools["strategize"]
        ),
        "plan": ComponentDefinition("plan.comparative", pools["plan"]),
        "execute": ComponentDefinition("execute.worktree", pools["execute"]),
        "evaluate": ComponentDefinition("evaluate.paired-suspect", ()),
        "reflect": ComponentDefinition("reflect.evidence", pools["reflect"]),
    }
    return agents, components


def _agent(name: str, value: Any) -> AgentDefinition:
    if not isinstance(value, Mapping) or set(value) != {"backend", "model", "settings"}:
        raise ValidationError(f"method.agents.{name} fields are invalid")
    backend = value["backend"]
    model = value["model"]
    settings = value["settings"]
    if not isinstance(backend, str) or not backend:
        raise ValidationError(f"method.agents.{name}.backend must be a non-empty string")
    if not isinstance(model, str) or not model:
        raise ValidationError(f"method.agents.{name}.model must be a non-empty string")
    if not isinstance(settings, Mapping) or set(settings) != {"reasoning_effort"}:
        raise ValidationError(f"method.agents.{name}.settings fields are invalid")
    effort = settings["reasoning_effort"]
    if effort not in _EFFORTS:
        raise ValidationError(
            f"method.agents.{name}.settings.reasoning_effort is invalid"
        )
    return AgentDefinition(name, backend, model, effort)


def parse_method(value: Any) -> MethodConfig:
    if not isinstance(value, Mapping):
        raise ValidationError("method must be an object")
    allowed = {"profile", "allow_unverified_isolation", "agents", "overrides"}
    if not {"profile", "allow_unverified_isolation"} <= set(value) <= allowed:
        raise ValidationError("method fields are invalid")
    profile = value["profile"]
    if profile not in {"serial", "serial-hotseat"}:
        raise ValidationError("method.profile is unsupported")
    allow_unverified = value["allow_unverified_isolation"]
    if not isinstance(allow_unverified, bool):
        raise ValidationError("method.allow_unverified_isolation must be boolean")
    agents, components = _defaults(profile)
    raw_agents = value.get("agents", {})
    if not isinstance(raw_agents, Mapping):
        raise ValidationError("method.agents must be an object")
    for name, raw in raw_agents.items():
        if not isinstance(name, str) or not name:
            raise ValidationError("method agent names must be non-empty strings")
        agents[name] = _agent(name, raw)
    overrides = value.get("overrides", {})
    if not isinstance(overrides, Mapping) or not set(overrides) <= _COMPONENT_NAMES:
        raise ValidationError("method.overrides names an unknown component")
    for name, raw in overrides.items():
        if not isinstance(raw, Mapping) or set(raw) != {"component", "agent_pool"}:
            raise ValidationError(f"method.overrides.{name} fields are invalid")
        identifier = raw["component"]
        resolve_component(name, identifier)
        pool = raw["agent_pool"]
        if not isinstance(pool, list) or any(
            not isinstance(item, str) or not item for item in pool
        ):
            raise ValidationError(f"method.overrides.{name}.agent_pool is invalid")
        if name in _AGENT_COMPONENTS:
            required = 2 if profile == "serial-hotseat" else 1
            invalid_size = (
                len(pool) < 2
                if profile == "serial-hotseat"
                else len(pool) != 1
            )
            if invalid_size or len(pool) != len(set(pool)):
                raise ValidationError(
                    f"method.overrides.{name}.agent_pool requires {required} unique agent(s)"
                )
        elif pool:
            raise ValidationError(f"method.overrides.{name} is controller-owned")
        if any(agent not in agents for agent in pool):
            raise ValidationError(f"method.overrides.{name} names an unknown agent")
        components[name] = ComponentDefinition(identifier, tuple(pool))
    return MethodConfig(profile, allow_unverified, agents, components)
