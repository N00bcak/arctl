"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Sequence

from .errors import ArctlError, StateError
from .models import validate_task_id
from .registry import locate_task
from .storage import TaskLock, atomic_write_text

_TASK_TEMPLATE = """\
# arctl task: edit the objective, paths, evaluator, and public commands, then approve.
schema_version: 1
task_id: {task_id}
repo: {repo}
objective: Describe the improvement you want.
editable_paths: [src/**, tests/**]
denied_paths: [.git/**, pyproject.toml, uv.lock]
public_checks: [[python, -m, pytest, -q]]
public_probe: [python, tools/dev_benchmark.py]
evaluator:
  repo: /absolute/path/to/private/evaluator
  commit: REPLACE_WITH_COMMIT
trials: auto
max_experiments: 30
"""


def _data_root(argument: Path | None) -> Path:
    if argument is not None:
        return argument.resolve()
    configured = os.environ.get("ARCTL_DATA")
    if configured:
        return Path(configured).resolve()
    return Path.home() / ".local" / "share" / "arctl"


def _payload(
    *,
    success: bool,
    state: str,
    task_id: str | None,
    action_required: bool,
    allowed_actions: Sequence[str],
    next_command: str,
    message: str,
    artifacts: Sequence[dict[str, str]] = (),
    evidence_valid: bool | None = None,
    can_continue: bool | None = True,
    log_path: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "success": success,
        "task_id": task_id,
        "experiment_id": None,
        "state": state,
        "action_required": action_required,
        "allowed_actions": list(allowed_actions),
        "artifacts": list(artifacts),
        "message": message,
        "evidence_valid": evidence_valid,
        "can_continue": can_continue,
        "log_path": log_path,
        "next_command": next_command,
    }


def _result_line(result: dict[str, Any]) -> str:
    from .dossier import safe_terminal_text

    comparisons = result.get("evaluation", {}).get("comparisons", [])
    final = comparisons[-1] if comparisons else {}
    measurement = (
        f" · effect {final.get('effect_estimate')} · lower bound "
        f"{final.get('one_sided_lower_bound')}"
        if final
        else ""
    )
    return (
        f"{result['experiment_id']:>3}  {result['decision']:<7}"
        f"{measurement} · {safe_terminal_text(result['hypothesis'], limit=180)}"
    )


def _emit_human(
    payload: dict[str, Any],
    *,
    show_artifacts: bool,
) -> None:
    from .dossier import safe_terminal_text

    state = payload["state"]
    if not payload["success"]:
        print(payload["message"], file=sys.stderr)
        if payload["evidence_valid"] is not None:
            print(
                "Saved evidence: "
                + ("valid and preserved." if payload["evidence_valid"] else "invalid."),
                file=sys.stderr,
            )
        if payload["log_path"] is not None:
            print(f"Details: {payload['log_path']}", file=sys.stderr)
    elif state == "REPORT":
        report = payload["report"]
        print(
            f"Task {payload['task_id']} · "
            f"{report['completed_experiments']} completed experiment(s)"
        )
        for result in report["results"]:
            print(_result_line(result))
            print(f"     Dossier: {result['dossier_path']}")
        if report["results"]:
            print(
                "\nNote: uncertainty is evaluator-owned; adaptive search has no "
                "task-wide false-promotion guarantee."
            )
    elif state == "INSPECT":
        result = payload.get("result")
        if result is None:
            print(payload["message"])
        else:
            print(f"Experiment {result['experiment_id']} · {result['decision']}")
            print(f"Hypothesis: {safe_terminal_text(result['hypothesis'])}")
            comparisons = result.get("evaluation", {}).get("comparisons", [])
            for comparison in comparisons:
                print(
                    f"{comparison['kind'].capitalize()}: "
                    f"{comparison['trials']} paired trial(s) · effect "
                    f"{comparison['effect_estimate']} · lower bound "
                    f"{comparison['one_sided_lower_bound']}"
                )
            print(f"Candidate: {result['candidate'][:12]}")
            print(f"Champion after: {result['champion_after'][:12]}")
            print(f"Dossier: {payload['dossier_path']}")
        if show_artifacts:
            print("\nSafe artifact inventory:")
            for artifact in payload["artifacts"]:
                print(f"- [{artifact['visibility']}] {artifact['path']}")
    elif state == "RUN_COMPLETE":
        results = payload.get("results", [])
        accepted = sum(result["decision"] == "ACCEPT" for result in results)
        print(
            f"Done: {len(results)} tested · {accepted} promoted · "
            f"{len(results) - accepted} not promoted"
        )
    elif state == "STOPPED":
        print(payload["message"])
    elif state == "TASK_DRAFT":
        print(payload["message"])
        for artifact in payload["artifacts"]:
            print(f"Edit: {artifact['path']}")
    elif state == "APPROVAL_REQUIRED":
        print(payload["message"])
    elif state == "READY" and "status" in payload:
        status = payload["status"]
        print(
            f"Task {payload['task_id']} · {status['state']} · "
            f"{status['trial_count'] or 'unfrozen'} trial(s)"
        )
        print(f"Champion: {(status['champion'] or 'none')[:12]}")
        if status["last_result"] is not None:
            print("Latest: " + _result_line(status["last_result"]).lstrip())
        if status["provisional"]:
            print("A candidate is provisional; its suspect test is pending.")
        if status["stop_requested"]:
            print("A safe stop has been requested.")
    else:
        print(payload["message"])
    print(f"Next: {payload['next_command']}")


def _emit(
    payload: dict[str, Any],
    *,
    as_json: bool,
    show_artifacts: bool = False,
) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    _emit_human(payload, show_artifacts=show_artifacts)


def _progress(event: dict[str, Any]) -> None:
    from .dossier import safe_terminal_text

    kind = event["event"]
    if kind == "calibration":
        print("Checking frozen calibration…", flush=True)
    elif kind == "ready":
        print(f"Official evaluation: {event['trial_count']} paired trial(s).", flush=True)
    elif kind == "experiment":
        print(f"\nExperiment {event['experiment_id']}", flush=True)
    elif kind == "research":
        print("  Researching from the current champion…", flush=True)
    elif kind == "candidate":
        print(
            f"  Proposed: {safe_terminal_text(event['claim'], limit=180)}",
            flush=True,
        )
        print(f"  Implemented candidate {event['candidate'][:12]}.", flush=True)
    elif kind == "public_checks":
        print("  Checking public constraints…", flush=True)
    elif kind == "public_checks_complete":
        outcome = "passed" if event["passed"] else "failed"
        print(f"  Public checks {outcome}.", flush=True)
    elif kind == "comparison":
        label = "Primary" if event["kind"] == "primary" else "Suspect"
        print(
            f"  {label} evaluation: {event['trial_count']} paired trial(s)…",
            flush=True,
        )
    elif kind == "provisional":
        print("  Apparent win flagged; champion unchanged pending suspect test.", flush=True)
    elif kind == "result":
        print("  " + _result_line(event["result"]).lstrip(), flush=True)


def _invoked_program(argv: Sequence[str] | None) -> str:
    if argv is not None:
        return "arctl"
    invoked = Path(sys.argv[0])
    if invoked.name == "__main__.py":
        return shlex.join((sys.executable, "-m", "arctl"))
    return shlex.quote(sys.argv[0])


def _rewrite_next_command(payload: dict[str, Any], program: str) -> None:
    command = payload.get("next_command")
    if isinstance(command, str) and (command == "arctl" or command.startswith("arctl ")):
        payload["next_command"] = program + command[5:]


def _doctor() -> dict[str, Any]:
    from .doctor import run_doctor

    checks = run_doctor()
    success = all(checks.values())
    failed = [name for name, passed in checks.items() if not passed]
    message = (
        "Runtime and Codex sandbox profile checks passed."
        if success
        else f"Runtime or sandbox checks failed: {', '.join(failed)}."
    )
    return {
        **_payload(
            success=success,
            state="DOCTOR_OK" if success else "DOCTOR_FAILED",
            task_id=None,
            action_required=not success,
            allowed_actions=("install",) if not success else ("init",),
            next_command="./install.sh" if not success else "arctl init --repo .",
            message=message,
        ),
        "checks": checks,
    }


def _init(repo_argument: Path, task_id: str | None, data_argument: Path | None) -> dict[str, Any]:
    repo = repo_argument.resolve()
    if not (repo / ".git").exists():
        raise StateError(f"target is not a Git worktree: {repo}")
    identifier = task_id or repo.name
    try:
        validate_task_id(identifier)
    except KeyboardInterrupt:
        if arguments.command != "run":
            raise
        from .operations import request_stop

        data_root = _data_root(arguments.data)
        task = _located(data_root, arguments.task_id)
        request_stop(task)
        payload = _run(data_root, arguments.task_id, arguments.max_experiments)
    except ArctlError as error:
        raise StateError("task ID contains unsafe path or Git-ref characters") from error
    task_directory = _data_root(data_argument) / "tasks" / identifier
    task_file = task_directory / "task.yaml"
    if task_file.exists():
        raise StateError(f"task already exists: {identifier}")
    task_directory.mkdir(parents=True, exist_ok=False)
    atomic_write_text(
        task_file,
        _TASK_TEMPLATE.format(
            task_id=json.dumps(identifier),
            repo=json.dumps(str(repo)),
        ),
    )
    return _payload(
        success=True,
        state="TASK_DRAFT",
        task_id=identifier,
        action_required=True,
        allowed_actions=("edit", "approve"),
        next_command=f"arctl approve {identifier}",
        message=f"Created starter task {identifier}; edit it before approval.",
        artifacts=({"kind": "task", "path": str(task_file)},),
    )


def _approve(
    *,
    data_root: Path,
    task_id: str | None,
    confirmation: str | None,
) -> dict[str, Any]:
    from .approval import confirm_approval, preview_approval

    located = locate_task(
        data_root,
        task_id=task_id,
        current_directory=Path.cwd(),
    )
    preview = preview_approval(located.directory / "task.yaml", located.config)
    if confirmation is None:
        command = (
            f"arctl approve {located.config.task_id} "
            f"--confirm {preview.confirmation_token}"
        )
        manifest = preview.manifest
        calibration = (
            f"{manifest.calibration.policy}; ceiling {manifest.calibration.ceiling}"
            if located.config.trials == "auto"
            else (
                f"skipped; fixed count {located.config.trials} will be used "
                "for every comparison"
            )
        )
        message = "\n".join(
            (
                f"Approval required for task {located.config.task_id}.",
                f"Task file: {located.directory / 'task.yaml'}",
                "Changes from prior approval: none (this is a new immutable task).",
                f"Editable paths: {', '.join(located.config.editable_paths)}",
                f"Denied paths: {', '.join(located.config.denied_paths)}",
                "Public checks: "
                + "; ".join(" ".join(command) for command in located.config.public_checks),
                f"Public probe: {' '.join(located.config.public_probe)}",
                f"Evaluator repo: {located.config.evaluator.repo}",
                f"Evaluator commit: {preview.evaluator_commit}",
                f"Manifest SHA-256: {preview.manifest_hash}",
                f"Trials: {located.config.trials}",
                f"Trial meaning: {manifest.trial_meaning}",
                f"Trial dependence: {manifest.trial_dependence}",
                f"Seed-to-case procedure: {manifest.seed_to_case}",
                "Subject-visible seeds: "
                + ("yes" if manifest.subject_visible_seed else "no"),
                f"Subject command: {' '.join(manifest.subject_command)}",
                f"Prepare command: {' '.join(manifest.prepare_command)}",
                f"Score command: {' '.join(manifest.score_command)}",
                f"Statistic: {manifest.score_statistic}",
                f"Positive effect: {manifest.positive_effect}",
                f"Uncertainty: {manifest.uncertainty_method}",
                f"Known variation: {manifest.known_variation}",
                f"Mitigations: {', '.join(manifest.variation_mitigations)}",
                f"Calibration: {calibration}",
                f"Suspect trigger: {manifest.suspect_trigger or 'none'}",
                "Suspect reason codes: "
                + (", ".join(manifest.suspect_reason_codes) or "none"),
                "Publishable telemetry: "
                + (", ".join(manifest.public_telemetry) or "none"),
                "This trusts the evaluator's statistical method; arctl validates "
                "the approved protocol and evidence shape, not its mathematics.",
                "Calibration and suspect testing do not provide a search-wide "
                "false-positive guarantee.",
                "An AI operator must obtain explicit human permission before confirming.",
            )
        )
        return {
            **_payload(
                success=True,
                state="APPROVAL_REQUIRED",
                task_id=located.config.task_id,
                action_required=True,
                allowed_actions=("approve",),
                next_command=command,
                message=message,
            ),
            "approval": {
                "task_sha256": preview.task_hash,
                "evaluator_commit": preview.evaluator_commit,
                "manifest_sha256": preview.manifest_hash,
                "confirmation_token": preview.confirmation_token,
            },
        }
    confirm_approval(
        located.directory,
        located.config,
        preview,
        confirmation,
    )
    return _payload(
        success=True,
        state="APPROVED",
        task_id=located.config.task_id,
        action_required=False,
        allowed_actions=("run", "status"),
        next_command=f"arctl run {located.config.task_id}",
        message=f"Approved and locked task {located.config.task_id}.",
    )


def _located(data_root: Path, task_id: str | None):
    return locate_task(
        data_root,
        task_id=task_id,
        current_directory=Path.cwd(),
    )


def _status(data_root: Path, task_id: str | None) -> dict[str, Any]:
    from .operations import task_status

    task = _located(data_root, task_id)
    status = task_status(task)
    identifier = task.config.task_id
    if status["state"] == "TASK_DRAFT":
        next_command = f"arctl approve {identifier}"
        action_required = True
    elif status["state"] == "CALIBRATION_FAILED":
        next_command = f"arctl status {identifier}"
        action_required = True
    elif status["state"] in ("RESEARCH_FAILED", "PUBLIC_CHECK_FAILED"):
        next_command = f"arctl run {identifier}"
        action_required = True
    elif status["state"] in ("READY", "CALIBRATION_REQUIRED"):
        next_command = f"arctl run {identifier}"
        action_required = False
    else:
        next_command = f"arctl status {identifier}"
        action_required = False
    last = status["last_result"]
    comparisons = (
        last.get("evaluation", {}).get("comparisons", []) if last is not None else []
    )
    effect = comparisons[-1].get("effect_estimate", "n/a") if comparisons else "n/a"
    last_text = (
        "none"
        if last is None
        else f"{last.get('decision')} (effect {effect})"
    )
    payload = _payload(
        success=True,
        state=status["state"],
        task_id=identifier,
        action_required=action_required,
        allowed_actions=(
            ("approve",)
            if status["state"] == "TASK_DRAFT"
            else (
                ("status", "report")
                if status["state"] == "CALIBRATION_FAILED"
                else ("run", "status", "stop", "report")
            )
        ),
        next_command=next_command,
        message=(
            f"Task {identifier}: {status['state']}; trial count "
            f"{status['trial_count'] or 'not frozen'}; last result {last_text}. "
            f"Logs: {status['log_path']}"
        ),
        log_path=status["log_path"],
    )
    payload["experiment_id"] = status["experiment_id"]
    payload["status"] = status
    return payload


def _report(data_root: Path, task_id: str | None) -> dict[str, Any]:
    from .operations import task_report

    task = _located(data_root, task_id)
    report = task_report(task)
    identifier = task.config.task_id
    return {
        **_payload(
            success=True,
            state="REPORT",
            task_id=identifier,
            action_required=False,
            allowed_actions=("run", "inspect", "status"),
            next_command=f"arctl status {identifier}",
            message=(
                f"Task {identifier} has {report['completed_experiments']} completed "
                f"experiments.\n{report['limitations']}"
            ),
            log_path=str(task.directory),
        ),
        "report": report,
    }


def _inspect(
    data_root: Path,
    task_id: str | None,
    experiment_id: int | None,
) -> dict[str, Any]:
    from .operations import inspect_experiment

    task = _located(data_root, task_id)
    inspection = inspect_experiment(task, experiment_id)
    identifier = task.config.task_id
    selected = inspection["experiment"]["experiment_id"]
    payload = _payload(
        success=True,
        state="INSPECT",
        task_id=identifier,
        action_required=False,
        allowed_actions=("status", "report"),
        next_command=f"arctl status {identifier}",
        message=(
            f"Experiment {selected} is {inspection['experiment']['state']}; "
            f"{len(inspection['artifacts'])} artifacts are recorded."
        ),
        artifacts=inspection["artifacts"],
        log_path=str(task.directory / "experiments" / f"{selected:06d}"),
    )
    payload["experiment_id"] = selected
    payload["result"] = inspection["result"]
    payload["dossier_path"] = inspection["dossier_path"]
    return payload


def _stop(data_root: Path, task_id: str | None) -> dict[str, Any]:
    from .operations import request_stop

    task = _located(data_root, task_id)
    created = request_stop(task)
    identifier = task.config.task_id
    return _payload(
        success=True,
        state="STOP_REQUESTED",
        task_id=identifier,
        action_required=False,
        allowed_actions=("status",),
        next_command=f"arctl status {identifier}",
        message=(
            f"Stop requested for task {identifier}."
            if created
            else f"Stop was already requested for task {identifier}."
        ),
        log_path=str(task.directory),
    )


def _run(
    data_root: Path,
    task_id: str | None,
    max_experiments: int | None,
    *,
    preflight: bool = True,
    progress=None,
) -> dict[str, Any]:
    from .doctor import run_doctor
    from .runner import run_task

    task = _located(data_root, task_id)
    if preflight:
        checks = run_doctor()
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise StateError(
                "runtime or sandbox preflight failed: " + ", ".join(failed)
            )
    with TaskLock(task.directory / "lock"):
        outcome = run_task(
            task,
            max_experiments=max_experiments,
            progress=progress,
        )
    results = outcome.results
    identifier = task.config.task_id
    last = results[-1] if results else None
    state = "STOPPED" if outcome.stopped else "RUN_COMPLETE"
    next_command = f"arctl status {identifier}"
    payload = _payload(
        success=True,
        state=state,
        task_id=identifier,
        action_required=False,
        allowed_actions=("status", "report", "inspect", "run"),
        next_command=next_command,
        message=(
            f"Task {identifier} stopped safely after {len(results)} experiments."
            if outcome.stopped
            else f"Task {identifier} completed {len(results)} experiments."
        ),
        evidence_valid=True if results else None,
        log_path=str(task.directory),
    )
    if last is not None:
        payload["experiment_id"] = last["experiment_id"]
    payload["results"] = results
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arctl")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--data", type=Path, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "approve", "run", "status", "stop", "report", "inspect"):
        command = subparsers.add_parser(name)
        command.add_argument("--json", action="store_true")
        if name not in ("doctor",):
            command.add_argument("task_id", nargs="?")
        if name == "run":
            command.add_argument("--max-experiments", type=int)
        if name == "approve":
            command.add_argument("--confirm")
        if name == "inspect":
            command.add_argument("experiment_id", nargs="?", type=int)
            command.add_argument("--artifacts", action="store_true")
    init = subparsers.add_parser("init")
    init.add_argument("--json", action="store_true")
    init.add_argument("--repo", type=Path, required=True)
    init.add_argument("--task-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    program = _invoked_program(argv)
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "doctor":
            payload = _doctor()
        elif arguments.command == "init":
            payload = _init(arguments.repo, arguments.task_id, arguments.data)
        elif arguments.command == "approve":
            payload = _approve(
                data_root=_data_root(arguments.data),
                task_id=arguments.task_id,
                confirmation=arguments.confirm,
            )
        elif arguments.command == "status":
            payload = _status(_data_root(arguments.data), arguments.task_id)
        elif arguments.command == "report":
            payload = _report(_data_root(arguments.data), arguments.task_id)
        elif arguments.command == "inspect":
            inspect_task = arguments.task_id
            inspect_experiment = arguments.experiment_id
            if (
                inspect_experiment is None
                and inspect_task is not None
                and inspect_task.isdecimal()
            ):
                explicit = (
                    _data_root(arguments.data) / "tasks" / inspect_task / "task.yaml"
                )
                if not explicit.is_file():
                    inspect_experiment = int(inspect_task)
                    inspect_task = None
            payload = _inspect(
                _data_root(arguments.data),
                inspect_task,
                inspect_experiment,
            )
        elif arguments.command == "stop":
            payload = _stop(_data_root(arguments.data), arguments.task_id)
        elif arguments.command == "run":
            payload = _run(
                _data_root(arguments.data),
                arguments.task_id,
                arguments.max_experiments,
                progress=None if arguments.json else _progress,
            )
        else:
            raise StateError(f"unsupported command: {arguments.command}")
    except KeyboardInterrupt:
        if arguments.command != "run":
            raise
        from .operations import request_stop

        data_root = _data_root(arguments.data)
        task = _located(data_root, arguments.task_id)
        request_stop(task)
        payload = _run(
            data_root,
            arguments.task_id,
            arguments.max_experiments,
            preflight=False,
            progress=None if arguments.json else _progress,
        )
    except ArctlError as error:
        if arguments.debug:
            raise
        identifier = getattr(arguments, "task_id", None)
        log_path = (
            str(_data_root(arguments.data) / "tasks" / identifier)
            if identifier is not None
            else None
        )
        if arguments.command == "doctor":
            next_command = "./install.sh"
        elif arguments.command == "init":
            next_command = "arctl doctor"
        else:
            next_command = (
                f"arctl status {identifier}" if identifier else "arctl status"
            )
        payload = _payload(
            success=False,
            state="ERROR",
            task_id=identifier,
            action_required=True,
            allowed_actions=("status",),
            next_command=next_command,
            message=(
                f"Failed: {error}."
            ),
            evidence_valid=True,
            can_continue=False,
            log_path=log_path,
        )
    _rewrite_next_command(payload, program)
    _emit(
        payload,
        as_json=arguments.json,
        show_artifacts=bool(
            arguments.command == "inspect" and arguments.artifacts
        ),
    )
    return 0 if payload["success"] else 1
