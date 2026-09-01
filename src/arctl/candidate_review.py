"""Pre-trial deterministic checks and independent candidate review."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaError

from .agent_backend import AgentSessionRequest, agent_command, agent_environment, agent_provenance
from .agent_selection import select_agent
from .downstream import transient_process_error
from .errors import ProcessError, ResearchMiss, StateError, StoppedError
from .git import normalize_runtime_artifacts
from .manifest import EvaluatorManifest
from .process import run_or_load_once
from .registry import LocatedTask
from .sandbox import (
    agent_prompt_path,
    command_runtime_read_paths,
    marked_command,
    sandbox_command,
    sanitized_environment,
)
from .storage import write_json_once

AgentCommandBuilder = Callable[[Path, Path, Path, str], Sequence[str]]
CheckCommandBuilder = Callable[[Sequence[str], Path, Path], Sequence[str]]
ProgressCallback = Callable[[dict[str, Any]], None]


def requirement_audit_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["requirement", "status", "evidence"],
            "properties": {
                "requirement": {"type": "string", "minLength": 1},
                "status": {
                    "type": "string",
                    "enum": ["verified", "unverified"],
                },
                "evidence": {"type": "string", "minLength": 1},
            },
        },
    }


def review_schema() -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    finding = {
        "type": "object",
        "additionalProperties": False,
        "required": ["rule", "evidence", "remediation"],
        "properties": {
            "rule": text,
            "evidence": text,
            "remediation": text,
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "summary", "findings"],
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "summary": text,
            "findings": {"type": "array", "items": finding},
        },
    }


def repair_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "status", "summary", "requirements"],
        "properties": {
            "schema_version": {"type": "integer", "const": 2},
            "status": {"type": "string", "enum": ["repaired", "infeasible"]},
            "summary": {"type": "string", "minLength": 1},
            "requirements": requirement_audit_schema(),
        },
    }


def _validate_repair(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("schema_version") == 1:
        legacy = {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema_version", "summary"],
            "properties": {
                "schema_version": {"type": "integer", "const": 1},
                "summary": {"type": "string", "minLength": 1},
            },
        }
        try:
            Draft202012Validator(legacy).validate(value)
        except JsonSchemaError as error:
            raise StateError("candidate repair did not write valid repair JSON") from error
        return {**value, "status": "repaired", "requirements": []}
    try:
        Draft202012Validator(repair_schema()).validate(value)
    except JsonSchemaError as error:
        raise StateError("candidate repair did not write valid repair JSON") from error
    assert isinstance(value, dict)
    if value["status"] == "repaired" and any(
        item["status"] != "verified" for item in value["requirements"]
    ):
        raise StateError("completed candidate repair has unverified requirements")
    return value


def _validate_review(value: Any) -> dict[str, Any]:
    legacy_verdict = value.get("verdict") if isinstance(value, dict) else None
    if legacy_verdict is not None:
        value = {key: item for key, item in value.items() if key != "verdict"}
    try:
        Draft202012Validator(review_schema()).validate(value)
    except JsonSchemaError as error:
        raise StateError("candidate reviewer did not write valid review JSON") from error
    assert isinstance(value, dict)
    verdict = "fail" if value["findings"] else "pass"
    if legacy_verdict is not None and legacy_verdict != verdict:
        raise StateError("legacy candidate review verdict contradicts its findings")
    return {**value, "verdict": verdict}


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"{label} did not write valid JSON") from error
    if not isinstance(value, dict):
        raise StateError(f"{label} did not write one JSON object")
    return value


def _agent_command(
    agent,
    *,
    worktree: Path,
    scratch: Path,
    schema: Path,
    prompt: str,
    output_name: str,
    writable: bool,
) -> Sequence[str]:
    return agent_command(
        agent,
        AgentSessionRequest(
            worktree=worktree,
            scratch=scratch,
            output_schema=schema,
            prompt=prompt,
            output_name=output_name,
            writable_worktree=writable,
        ),
    )


def _run_agent(
    task: LocatedTask,
    *,
    worktree: Path,
    scratch: Path,
    schema_value: Mapping[str, Any],
    prompt: str,
    output_name: str,
    command_builder: AgentCommandBuilder | None,
    writable: bool,
    stop_path: Path,
) -> dict[str, Any]:
    agent_root = scratch
    attempts = agent_root / "attempts"
    existing = sorted(attempts.glob("[0-9][0-9][0-9][0-9]"))
    legacy_started = (agent_root / "process" / "started.json").is_file()
    attempt_number = len(existing) + 1 + int(legacy_started)
    scratch = attempts / f"{attempt_number:04d}"
    scratch.mkdir(parents=True, exist_ok=True)
    schema = scratch / "output.schema.json"
    write_json_once(schema, schema_value)
    agent = None
    if command_builder is None:
        assert task.config.method is not None
        role = "repair" if writable else "review"
        agent = select_agent(
            task.config.method,
            component="execute",
            lifecycle=f"{role}:{agent_root.parent.name}:{agent_root.name}",
            root=agent_root,
        )
    try:
        command = (
            command_builder(worktree, scratch, schema, prompt)
            if command_builder is not None
            else _agent_command(
                agent,
                worktree=worktree,
                scratch=scratch,
                schema=schema,
                prompt=prompt,
                output_name=output_name,
                writable=writable,
            )
        )
    except (OSError, StateError) as error:
        write_json_once(
            scratch / "failure.json",
            {"schema_version": 1, "message": str(error)},
        )
        raise
    process_directory = scratch / "process"
    if command_builder is None:
        write_json_once(
            scratch / "agent.public.json",
            agent_provenance(
                agent,
                lifecycle=f"{'repair' if writable else 'review'}:"
                f"{agent_root.parent.name}:{agent_root.name}",
            ),
        )
    try:
        result = run_or_load_once(
            process_directory,
            command,
            timeout_seconds=600,
            max_output_bytes=1_000_000,
            cwd=worktree,
            env=(
                None
                if command_builder is not None
                else agent_environment(
                    agent,
                    credential_home=Path(
                        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
                    ),
                    writable_home=scratch,
                )
            ),
            stop_path=stop_path,
            stdin_path=(
                None if command_builder is not None else agent_prompt_path(scratch)
            ),
        )
    except ProcessError as error:
        transient = transient_process_error(
            process_directory,
            stage=(
                "policy repair"
                if output_name == "repair.public.json"
                else "policy review"
            ),
            codex=command_builder is None,
            fallback=str(error),
        )
        if transient is not None:
            raise transient from error
        raise
    if result["return_code"] != 0:
        transient = transient_process_error(
            process_directory,
            stage=(
                "policy repair"
                if output_name == "repair.public.json"
                else "policy review"
            ),
            codex=command_builder is None,
        )
        if transient is not None:
            raise transient
        raise StateError(f"fresh candidate {output_name.removesuffix('.public.json')} session exited unsuccessfully")
    return _load_json(scratch / output_name, label="candidate agent")


def _check_failure(
    task: LocatedTask,
    worktree: Path,
    root: Path,
    manifest: EvaluatorManifest,
    *,
    artifact_audit: Path,
    artifact_stage: str,
    command_builder: CheckCommandBuilder | None,
    stop_path: Path,
) -> dict[str, Any] | None:
    assert task.config.candidate_review is not None
    for index, command in enumerate(task.config.candidate_review.checks, start=1):
        checks = root / "checks"
        scratch = checks / f"{index:04d}"
        if transient_process_error(
            scratch / "process",
            stage=f"policy check {index}",
            codex=False,
        ) is not None:
            retries = sorted(checks.glob(f"{index:04d}-retry-*"))
            scratch = checks / f"{index:04d}-retry-{len(retries) + 1:04d}"
        scratch.mkdir(parents=True, exist_ok=True)
        marker = scratch / "execution.started"
        if command_builder is None:
            managed = sandbox_command(
                marked_command(command, marker),
                cwd=worktree,
                read_paths=command_runtime_read_paths(command),
                write_paths=(worktree, scratch),
                profile="arctl-research",
            )
            home = scratch / "home"
            codex_home = scratch / "codex-home"
            home.mkdir(exist_ok=True)
            codex_home.mkdir(exist_ok=True)
            environment = sanitized_environment(
                codex_home=codex_home, writable_home=home
            )
        else:
            managed = command_builder(command, worktree, scratch)
            environment = None
        try:
            result = run_or_load_once(
                scratch / "process",
                managed,
                timeout_seconds=manifest.limits.timeout_seconds,
                max_output_bytes=manifest.limits.max_output_bytes,
                cwd=worktree,
                env=environment,
                stop_path=stop_path,
            )
        except StoppedError:
            raise
        except (ProcessError, StateError) as error:
            if command_builder is None and not marker.is_file():
                raise StateError("candidate-check sandbox did not start its command") from error
            transient = transient_process_error(
                scratch / "process",
                stage=f"policy check {index}",
                codex=False,
                fallback=str(error),
            )
            if transient is not None:
                raise transient from error
            result = {"return_code": 1}
        normalize_runtime_artifacts(
            worktree,
            stage=f"{artifact_stage}/check-{scratch.name}",
            audit_path=artifact_audit,
        )
        if result["return_code"] == 0:
            continue
        transient = transient_process_error(
            scratch / "process",
            stage=f"policy check {index}",
            codex=False,
        )
        if transient is not None:
            raise transient
        output = ""
        for name in ("stdout.bin", "stderr.bin"):
            path = scratch / "process" / name
            if path.is_file():
                output += path.read_text(encoding="utf-8", errors="replace")
        detail = output.strip()[:4000] or f"candidate check {index} failed"
        return {
            "schema_version": 1,
            "verdict": "fail",
            "summary": f"Deterministic candidate check {index} failed.",
            "findings": [
                {
                    "rule": f"candidate_check_{index}",
                    "evidence": detail,
                    "remediation": "Remove the prohibited capability or repair the policy interface.",
                }
            ],
        }
    return None


def _review_prompt(
    task: LocatedTask,
    *,
    subject_interface: str,
    champion: str,
    request: Mapping[str, Any],
    implementation_report: Mapping[str, Any] | None,
    compute_report: Mapping[str, Any] | None,
) -> str:
    assert task.config.candidate_review is not None
    packet = {
        "objective": task.config.objective,
        "champion": champion,
        "editable_paths": list(task.config.editable_paths),
        "denied_paths": list(task.config.denied_paths),
        "contract": task.config.candidate_review.contract,
        "subject_interface": subject_interface,
        "research_request": request,
        "implementation_audit": implementation_report,
        "controller_compute_probe": compute_report,
    }
    return (
        "Independently review the uncommitted champion-to-candidate diff before any "
        "trial runs. Inspect the changed implementation and its trusted public interface. "
        "Pass only when the candidate obeys the contract, uses no privileged information "
        "or side channel, remains deterministic for the same observable history, and "
        "implements the declared research mechanism. Independently check whether the "
        "implementation audit covers every material obligation and whether each claimed "
        "verification matches the code. Continue after finding a violation and report "
        "every independently supported violation you can establish in this review. Do "
        "not require a particular code structure when different code has the required "
        "behavior. Do not edit anything. Cite concrete paths, constructs, and behavioral "
        "consequences in each finding. The findings array is exclusively for "
        "contract violations that require remediation. Leave it empty when there are "
        "no violations, and put evidence supporting a clean review in the summary. "
        "The controller derives the verdict from whether findings is empty. Return "
        "only the required review JSON.\n\n"
        + json.dumps(packet, sort_keys=True, separators=(",", ":"))
    )


def _repair_prompt(
    task: LocatedTask,
    *,
    subject_interface: str,
    request: Mapping[str, Any],
    review: Mapping[str, Any],
    implementation_report: Mapping[str, Any] | None,
) -> str:
    assert task.config.candidate_review is not None
    packet = {
        "contract": task.config.candidate_review.contract,
        "subject_interface": subject_interface,
        "research_request": request,
        "review": review,
        "prior_implementation_audit": implementation_report,
    }
    return (
        "Repair every cited candidate-review violation in the current worktree, then "
        "re-audit the complete frozen request sentence by sentence. Extract every "
        "behavioral, fallback, validation, and fidelity obligation into the requirements "
        "checklist. Inspect the relevant trusted interface, run applicable public checks "
        "and targeted probes, and keep fixing until every requirement has concrete code "
        "or test evidence. Preserve the research claim and mechanism; do not broaden "
        "scope, commit, or touch denied paths. Return status repaired only when every "
        "requirement is verified; otherwise return infeasible. Emit schema version 2 and "
        "only the required repair JSON after editing.\n\n"
        + json.dumps(packet, sort_keys=True, separators=(",", ":"))
    )


def review_candidate(
    task: LocatedTask,
    manifest: EvaluatorManifest,
    *,
    worktree: Path,
    attempt_directory: Path,
    champion: str,
    request: Mapping[str, Any],
    stop_path: Path,
    implementation_report: Mapping[str, Any] | None = None,
    compute_report: Mapping[str, Any] | None = None,
    review_command_builder: AgentCommandBuilder | None = None,
    repair_command_builder: AgentCommandBuilder | None = None,
    check_command_builder: CheckCommandBuilder | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any] | None:
    """Require a clean final review, allowing the configured bounded repair."""
    config = task.config.candidate_review
    if config is None:
        return None
    rounds = config.repair_attempts + 1
    for round_number in range(1, rounds + 1):
        root = attempt_directory / "candidate-review" / f"round-{round_number:02d}"
        if progress is not None:
            progress({"event": "candidate_review", "round": round_number, "rounds": rounds})
        review = _check_failure(
            task,
            worktree,
            root,
            manifest,
            artifact_audit=(
                attempt_directory / "runtime-artifacts.public.json"
            ),
            artifact_stage=f"candidate-review/{root.name}",
            command_builder=check_command_builder,
            stop_path=stop_path,
        )
        if review is None:
            prompt = _review_prompt(
                task,
                subject_interface=manifest.subject_interface,
                champion=champion,
                request=request,
                implementation_report=implementation_report,
                compute_report=compute_report,
            )
            raw = _run_agent(
                task,
                worktree=worktree,
                scratch=root / "semantic",
                schema_value=review_schema(),
                prompt=prompt,
                output_name="review.public.json",
                command_builder=review_command_builder,
                writable=False,
                stop_path=stop_path,
            )
            normalize_runtime_artifacts(
                worktree,
                stage=f"candidate-review/{root.name}/semantic",
                audit_path=attempt_directory / "runtime-artifacts.public.json",
            )
            review = _validate_review(raw)
            assert review is not None
        write_json_once(root / "decision.public.json", review)
        if review["verdict"] == "pass":
            return review
        if round_number == rounds:
            finding = review["findings"][0]
            raise ResearchMiss(
                "policy_review_failed",
                f"{finding['rule']} — {finding['evidence']}",
                details={"candidate_review": review},
            )
        if progress is not None:
            progress({"event": "candidate_repair", "attempt": round_number, "attempts": config.repair_attempts})
        repair = _run_agent(
            task,
            worktree=worktree,
            scratch=root / "repair",
            schema_value=repair_schema(),
            prompt=_repair_prompt(
                task,
                subject_interface=manifest.subject_interface,
                request=request,
                review=review,
                implementation_report=implementation_report,
            ),
            output_name="repair.public.json",
            command_builder=repair_command_builder,
            writable=True,
            stop_path=stop_path,
        )
        normalize_runtime_artifacts(
            worktree,
            stage=f"candidate-review/{root.name}/repair",
            audit_path=attempt_directory / "runtime-artifacts.public.json",
        )
        implementation_report = _validate_repair(repair)
        if implementation_report["status"] == "infeasible":
            raise ResearchMiss(
                "candidate_repair_infeasible",
                implementation_report["summary"],
            )
    raise AssertionError("candidate review loop did not terminate")
