"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import threading
import time
from pathlib import Path
from typing import Any, TextIO, Sequence

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
strategy:
  model: gpt-5.6-sol
  reasoning_effort: high
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


def _calibration_warning(summary: dict[str, Any] | None) -> str | None:
    if summary is None or summary["criterion_met"]:
        return None
    return (
        "Warning: calibration did not meet "
        f"{summary['diagnostic']} ≤ {summary['maximum']} {summary['units']}; "
        "the approved ceiling was used."
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
        warning = _calibration_warning(report.get("calibration_summary"))
        if warning is not None:
            print("\n" + warning)
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
        warning = _calibration_warning(payload.get("calibration_summary"))
        if warning is not None:
            print(warning)
    elif state == "RUN_COMPLETE":
        results = payload.get("results", [])
        accepted = sum(result["decision"] == "ACCEPT" for result in results)
        print(
            f"Done: {len(results)} tested · {accepted} promoted · "
            f"{len(results) - accepted} not promoted"
        )
    elif state == "STOPPED":
        print(payload["message"])
    elif state == "SEARCH_STALLED":
        print(payload["message"])
        print("Review with: arctl history " + str(payload["task_id"]))
    elif state == "HISTORY":
        history = payload["history"]
        print(f"Exploration history · {history['count']} matching entrie(s)")
        for entry in history["entries"]:
            detail = entry.get("claim") or entry.get("kind")
            outcome = entry.get("decision") or entry.get("rejection_code") or "saved"
            print(f"- {entry['entry_id']} · {outcome} · {safe_terminal_text(str(detail))}")
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
        if status.get("strategy_revision"):
            print(f"Strategy: revision {status['strategy_revision']}")
        if status.get("search_id") is not None:
            attempt = status.get("search_attempt") or 0
            print(f"Candidate search: {status['search_id']} · {attempt}/6 attempt(s)")
        if status["last_result"] is not None:
            print("Latest: " + _result_line(status["last_result"]).lstrip())
        if status["provisional"]:
            print("A candidate is provisional; its suspect test is pending.")
        if status["stop_requested"]:
            print("A safe stop has been requested.")
        warning = _calibration_warning(status.get("calibration_summary"))
        if warning is not None:
            print(warning)
    else:
        print(payload["message"])


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


class _ProgressView:
    """Render live FSM stages without making timing part of official evidence."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        clock=time.monotonic,
        interactive: bool | None = None,
    ) -> None:
        self.stream = sys.stdout if stream is None else stream
        self.clock = clock
        self.interactive = (
            self.stream.isatty() if interactive is None else interactive
        )
        self._active: tuple[str, float, str] | None = None
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        if self.interactive:
            self._thread = threading.Thread(target=self._refresh, daemon=True)
            self._thread.start()

    @staticmethod
    def _duration(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes, remainder = divmod(int(seconds), 60)
        return f"{minutes}m {remainder:02d}s"

    def _clear_active(self) -> None:
        if self.interactive and self._active is not None:
            self.stream.write("\r\033[2K")

    def _line(self, text: str) -> None:
        self._clear_active()
        self.stream.write(text + "\n")
        self.stream.flush()

    def _start(self, label: str, *, indent: str = "  ") -> None:
        if self._active is not None:
            self._finish("complete")
        self._active = (label, self.clock(), indent)
        if not self.interactive:
            self._line(f"{indent}› {label}")

    def _finish(self, status: str = "complete") -> None:
        if self._active is None:
            return
        label, started, indent = self._active
        elapsed = self._duration(max(0.0, self.clock() - started))
        self._clear_active()
        marker = {"recovered": "↺", "failed": "✗"}.get(status, "✓")
        suffix = (
            " · recovered"
            if status == "recovered"
            else f" · {elapsed}" + (" · failed" if status == "failed" else "")
        )
        self.stream.write(f"{indent}{marker} {label}{suffix}\n")
        self.stream.flush()
        self._active = None

    def _refresh(self) -> None:
        while not self._closed.wait(0.2):
            with self._lock:
                if self._active is None:
                    continue
                label, started, indent = self._active
                elapsed = self._duration(max(0.0, self.clock() - started))
                self.stream.write(f"\r\033[2K{indent}› {label} · {elapsed}")
                self.stream.flush()

    def close(self, *, failed: bool = False) -> None:
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        with self._lock:
            self._finish("failed" if failed else "complete")

    def __call__(self, event: dict[str, Any]) -> None:
        from .dossier import safe_terminal_text

        with self._lock:
            kind = event["event"]
            if kind == "calibration":
                self._line("Calibration")
            elif kind == "ready":
                self._finish()
                self._line(
                    f"Official evaluation: {event['trial_count']} paired trial(s)."
                )
                warning = _calibration_warning(event.get("calibration_summary"))
                if warning is not None:
                    self._line(warning)
            elif kind == "experiment":
                self._finish()
                self._line(f"\nExperiment {event['experiment_id']}")
            elif kind == "strategy":
                self._finish()
                label = "Strategy refresh" if event["refresh"] else "Strategy"
                self._line(f"{label} · revision {event['revision']}")
            elif kind == "search_attempt":
                self._start(
                    f"candidate search · attempt {event['attempt']}/{event['attempts']}"
                )
            elif kind == "search_miss":
                self._finish("failed")
                self._line(
                    "    Miss: "
                    + safe_terminal_text(event["message"], limit=180)
                )
            elif kind == "research":
                self._start("RESEARCHING")
            elif kind == "candidate":
                self._finish()
                self._line("  ✓ CANDIDATE_FROZEN")
                self._line(
                    "    Proposed: "
                    + safe_terminal_text(event["claim"], limit=180)
                )
                self._line(f"    Candidate: {event['candidate'][:12]}")
            elif kind == "public_checks":
                self._start("public checks", indent="    ")
            elif kind == "public_checks_complete":
                self._finish()
                outcome = "passed" if event["passed"] else "failed"
                self._line(f"    Public checks {outcome}.")
            elif kind == "comparison":
                self._finish()
                label = (
                    "PRIMARY_RESERVED"
                    if event["kind"] == "primary"
                    else "SUSPECT_RESERVED"
                )
                self._line(
                    f"  › {label} · {event['trial_count']} paired trial(s)"
                )
            elif kind == "stage":
                label = self._stage_label(event)
                if event["status"] == "started":
                    self._start(label, indent="      ")
                elif self._active is not None:
                    self._finish(event["status"])
                else:
                    marker = "↺" if event["status"] == "recovered" else "✓"
                    self._line(f"      {marker} {label}")
            elif kind == "provisional":
                self._finish()
                self._line("  ✓ PROVISIONAL · suspect comparison required")
            elif kind == "reflection":
                self._finish()
                self._start("REFLECTING")
            elif kind == "reflection_complete":
                self._finish()
            elif kind == "reflection_failed":
                self._finish("failed")
            elif kind == "result":
                self._finish()
                self._line("  ✓ FINALIZING")
                self._line("  ✓ COMPLETE")
                self._line("    " + _result_line(event["result"]).lstrip())
            elif kind == "complete":
                self._finish()

    @staticmethod
    def _stage_label(event: dict[str, Any]) -> str:
        stage = event["stage"]
        if event["scope"] == "calibration":
            labels = {
                "reserve": "reserve calibration seeds",
                "prepare": "evaluator prepare",
                "champion_pilot": "champion pilot",
                "assessment": "evaluator assessment",
                "freeze": "freeze trial count",
            }
            label = labels[stage]
        else:
            if stage == "subject":
                label = f"subject batch {event['batch']}/{event['batches']}"
            else:
                label = {
                    "comparison": "saved comparison",
                    "prepare": "evaluator prepare",
                    "score": "evaluator score",
                    "validate": "validate evidence",
                }[stage]
        if "trial_count" in event and stage in ("champion_pilot", "subject"):
            label += f" · {event['trial_count']} trials"
        return label


def _progress(event: dict[str, Any]) -> None:
    """Compatibility helper for direct callers and focused rendering tests."""
    view = _ProgressView(interactive=False)
    try:
        view(event)
    finally:
        view.close()


def _invoked_program(argv: Sequence[str] | None) -> str:
    if argv is not None:
        return "arctl"
    invoked = Path(sys.argv[0])
    if invoked.name == "__main__.py":
        return shlex.join((sys.executable, "-m", "arctl"))
    return shlex.quote(sys.argv[0])


def _rewrite_next_command(
    payload: dict[str, Any],
    program: str,
    data_root: Path | None = None,
) -> None:
    command = payload.get("next_command")
    if isinstance(command, str) and (command == "arctl" or command.startswith("arctl ")):
        prefix = program
        if data_root is not None:
            prefix += " --data " + shlex.quote(str(data_root.resolve()))
        payload["next_command"] = prefix + command[5:]


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
            (
                f"{manifest.calibration.policy}; ladder "
                f"{list(manifest.calibration.ladder)}; diagnostic "
                f"{manifest.calibration.diagnostic_name} ≤ "
                f"{manifest.calibration.diagnostic_maximum} "
                f"{manifest.calibration.diagnostic_units}; use the ceiling with "
                "a persistent warning if no rung meets the target"
            )
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
                "Strategy model: "
                f"{located.config.strategy_model} ({located.config.strategy_reasoning_effort})",
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
                + (
                    "; ".join(
                        f"{name} [{metric.scope}/{metric.role}, {metric.unit}, "
                        f"{metric.direction}]: {metric.description}"
                        for name, metric in manifest.public_telemetry.items()
                    )
                    or "none"
                ),
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
    elif status["state"] in (
        "RESEARCH_FAILED",
        "STRATEGY_FAILED",
        "PUBLIC_CHECK_FAILED",
        "REFLECTION_FAILED",
        "SEARCH_STALLED",
    ):
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
                else ("run", "status", "stop", "report", "history")
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
    state = (
        "STOPPED"
        if outcome.stopped
        else "REFLECTION_FAILED"
        if outcome.reflection_failed
        else "SEARCH_STALLED"
        if outcome.stalled
        else "RUN_COMPLETE"
    )
    next_command = f"arctl status {identifier}"
    payload = _payload(
        success=not outcome.reflection_failed,
        state=state,
        task_id=identifier,
        action_required=outcome.stalled or outcome.reflection_failed,
        allowed_actions=("status", "history", "report", "inspect", "run"),
        next_command=next_command,
        message=(
            f"Task {identifier} stopped safely after {len(results)} experiments."
            if outcome.stopped
            else (
                f"Post-trial reflection failed for {identifier}; valid statistical "
                "evidence was preserved and no further research was started."
                if outcome.reflection_failed
                else
                f"Candidate search for {identifier} stalled after six attempts; "
                "the exploration history was preserved."
                if outcome.stalled
                else f"Task {identifier} completed {len(results)} experiments."
            )
        ),
        evidence_valid=True if results else None,
        log_path=str(task.directory),
    )
    if last is not None:
        payload["experiment_id"] = last["experiment_id"]
    payload["results"] = results
    return payload


def _history(
    data_root: Path,
    task_id: str | None,
    *,
    query: str | None,
    path: str | None,
    decision: str | None,
) -> dict[str, Any]:
    from .operations import exploration_history

    task = _located(data_root, task_id)
    history = exploration_history(task, query=query, path=path, decision=decision)
    return {
        **_payload(
            success=True,
            state="HISTORY",
            task_id=task.config.task_id,
            action_required=False,
            allowed_actions=("history", "run", "status"),
            next_command=f"arctl status {task.config.task_id}",
            message=f"Found {history['count']} exploration entries.",
            log_path=history["ledger_path"],
        ),
        "history": history,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arctl")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--data", type=Path, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "approve", "run", "status", "stop", "report", "inspect", "history"):
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
        if name == "history":
            command.add_argument("--query")
            command.add_argument("--path")
            command.add_argument("--decision")
    init = subparsers.add_parser("init")
    init.add_argument("--json", action="store_true")
    init.add_argument("--repo", type=Path, required=True)
    init.add_argument("--task-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    program = _invoked_program(argv)
    arguments = build_parser().parse_args(argv)
    progress_view = (
        _ProgressView()
        if arguments.command == "run" and not arguments.json
        else None
    )
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
        elif arguments.command == "history":
            payload = _history(
                _data_root(arguments.data),
                arguments.task_id,
                query=arguments.query,
                path=arguments.path,
                decision=arguments.decision,
            )
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
                progress=progress_view,
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
            progress=progress_view,
        )
    except ArctlError as error:
        if arguments.debug:
            if progress_view is not None:
                progress_view.close(failed=True)
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
    if progress_view is not None:
        progress_view.close(failed=not payload["success"])
    _rewrite_next_command(payload, program, arguments.data)
    _emit(
        payload,
        as_json=arguments.json,
        show_artifacts=bool(
            arguments.command == "inspect" and arguments.artifacts
        ),
    )
    return 0 if payload["success"] else 1
