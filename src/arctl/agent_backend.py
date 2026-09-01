"""Installed agent-backend adapters and per-session provenance."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .errors import StateError, ValidationError
from .methods import AgentDefinition, MethodConfig
from .sandbox import research_command, sanitized_environment

@dataclass(frozen=True)
class AgentSessionRequest:
    worktree: Path
    scratch: Path
    output_schema: Path
    prompt: str
    output_name: str
    read_paths: tuple[Path, ...] = ()
    write_paths: tuple[Path, ...] = ()
    writable_worktree: bool = False
    read_worktree: bool = True
    network_enabled: bool = False


@dataclass(frozen=True)
class BackendAdapter:
    identifier: str
    version: str
    certification: str
    conformance_suite: str
    capabilities: frozenset[str]
    command: Callable[[AgentDefinition, AgentSessionRequest], tuple[str, ...]]
    environment: Callable[[Path, Path], dict[str, str]]


def _codex_command(
    agent: AgentDefinition, request: AgentSessionRequest
) -> tuple[str, ...]:
    return research_command(
        worktree=request.worktree,
        scratch=request.scratch,
        output_schema=request.output_schema,
        prompt=request.prompt,
        read_paths=request.read_paths,
        write_paths=request.write_paths,
        output_name=request.output_name,
        model=agent.model,
        reasoning_effort=agent.reasoning_effort,
        writable_worktree=request.writable_worktree,
        read_worktree=request.read_worktree,
        network_enabled=request.network_enabled,
    )


def _codex_environment(credential_home: Path, writable_home: Path) -> dict[str, str]:
    return sanitized_environment(
        codex_home=credential_home,
        writable_home=writable_home,
    )


BACKEND_ADAPTERS: dict[str, BackendAdapter] = {
    "codex-cli-v1": BackendAdapter(
        identifier="codex-cli-v1",
        version="1",
        certification="verified",
        conformance_suite="arctl-agent-conformance-v1",
        capabilities=frozenset(
            {
                "fresh_session",
                "structured_output",
                "workspace_read",
                "workspace_write",
                "tool_execution",
                "user_config_suppression",
                "network_mediation",
            }
        ),
        command=_codex_command,
        environment=_codex_environment,
    )
}


def adapter_for(agent: AgentDefinition) -> BackendAdapter:
    try:
        return BACKEND_ADAPTERS[agent.backend]
    except KeyError as error:
        raise StateError(f"agent backend adapter is not installed: {agent.backend}") from error


def agent_command(agent: AgentDefinition, request: AgentSessionRequest) -> tuple[str, ...]:
    return adapter_for(agent).command(agent, request)


def agent_environment(
    agent: AgentDefinition,
    *,
    credential_home: Path,
    writable_home: Path,
) -> dict[str, str]:
    return adapter_for(agent).environment(credential_home, writable_home)


def agent_provenance(agent: AgentDefinition, *, lifecycle: str) -> dict[str, object]:
    backend = adapter_for(agent)
    return {
        "schema_version": 2,
        "lifecycle": lifecycle,
        "agent": agent.name,
        "backend": backend.identifier,
        "adapter_version": backend.version,
        "certification": backend.certification,
        "conformance_suite": backend.conformance_suite,
        "model": agent.model,
        "settings": {"reasoning_effort": agent.reasoning_effort},
        "capabilities": sorted(backend.capabilities),
    }


def validate_method_backends(method: MethodConfig) -> dict[str, dict[str, object]]:
    """Validate installed adapters and return immutable approval-time attestations."""
    attestations: dict[str, dict[str, object]] = {}
    referenced = {
        name
        for component in method.components.values()
        for name in component.agent_pool
    }
    for name in sorted(referenced):
        agent = method.agents[name]
        try:
            backend = adapter_for(agent)
        except StateError as error:
            raise ValidationError(str(error)) from error
        required = {"fresh_session", "structured_output", "workspace_read"}
        if agent.name in method.components["execute"].agent_pool:
            required.add("workspace_write")
        missing = required - backend.capabilities
        if missing:
            raise ValidationError(
                f"agent backend {agent.backend} lacks capabilities: {sorted(missing)}"
            )
        if backend.certification != "verified" and not method.allow_unverified_isolation:
            raise ValidationError(
                f"agent backend {agent.backend} requires unverified isolation approval"
            )
        attestations[agent.backend] = {
            "adapter_version": backend.version,
            "certification": backend.certification,
            "conformance_suite": backend.conformance_suite,
            "capabilities": sorted(backend.capabilities),
        }
    return dict(sorted(attestations.items()))
