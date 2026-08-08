"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any, Callable, TextIO, Sequence

from .errors import ArctlError, StateError, TransientDownstreamError
from .git import resolve_commit
from .models import validate_task_id
from .registry import locate_task
from .storage import TaskLock, atomic_write_json, atomic_write_text

_TASK_TEMPLATE = """\
# arctl task: edit the objective, paths, evaluator, and public commands, then approve.
schema_version: 4
task_id: {task_id}
repo: {repo}
objective: Describe the improvement you want.
editable_paths: [src/**, tests/**]
denied_paths: [.git/**, pyproject.toml, uv.lock]
public_checks: [[python, -m, pytest, -q]]
public_probe: [python, tools/dev_benchmark.py]
environment:
  codebases:
    - id: environment-core
      repo: {repo}
      commit: {environment_commit}
      include: [ENVIRONMENT.md]
      description: Public environment implementation, interface, and rules.
  probes: []
evaluator:
  repo: /absolute/path/to/private/evaluator
  commit: REPLACE_WITH_COMMIT
method:
  profile: serial-v1
  allow_unverified_isolation: false
trials: auto
max_experiments: 1000
"""


def _data_root(argument: Path | None) -> Path:
    if argument is not None:
        return argument.resolve()
    configured = os.environ.get("ARCTL_DATA")
    if configured:
        return Path(configured).resolve()
    current = Path.cwd().resolve()
    for parent in (current, *current.parents):
        local = parent / ".arctl-data"
        if local.is_dir():
            return local.resolve()
    completed = subprocess.run(
        ["git", "-C", str(current), "config", "--local", "--get", "arctl.dataRoot"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        return Path(completed.stdout.strip()).resolve()
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
    from .results import normalize_result_statuses

    result = normalize_result_statuses(result)
    comparisons = result.get("evaluation", {}).get("comparisons", [])
    final = comparisons[-1] if comparisons else {}
    if not final and result.get("failure_detail"):
        return (
            f"{result['experiment_id']:>3}  {result['decision']:<7} · "
            f"{result['operational_status']} / {result['scientific_status']} · "
            "no score: "
            + safe_terminal_text(result["failure_detail"], limit=100)
        )
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


def _short_number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"{value:.5g}"


def _champion_display(commit: Any, provenance: Any) -> str:
    from .dossier import safe_terminal_text

    if not isinstance(commit, str) or not commit:
        return "None"
    first = commit[:12]
    if isinstance(provenance, dict) and provenance.get("kind") == "experiment":
        first += f" (Expt #{provenance.get('experiment_id')})"
        hypothesis = provenance.get("hypothesis")
        if isinstance(hypothesis, str) and hypothesis:
            return first + "\n" + safe_terminal_text(hypothesis, limit=180)
        return first
    if isinstance(provenance, dict) and provenance.get("kind") == "initial":
        return first + " (Initial champion)"
    return first + " (Origin unknown)"


def _status_table(payload: dict[str, Any]) -> str:
    from tabulate import tabulate

    from .dossier import safe_terminal_text

    status = payload["status"]
    count = status.get("trial_count")
    calibration = status.get("calibration")
    if count is not None:
        qualifier = {
            "complete": " (autocalibrated)",
            "fixed": " (fixed)",
        }.get(calibration, "")
        trials = f"{count} paired trials{qualifier}"
    elif calibration in ("not_started", None):
        trials = "Unfrozen (to be autocalibrated)"
    else:
        trials = "Unfrozen (autocalibration failed)"
    rows: list[tuple[str, str]] = [
        ("Task", safe_terminal_text(payload["task_id"])),
        ("State", safe_terminal_text(status["state"])),
        ("Trials / Expt", trials),
        (
            "Champion",
            _champion_display(
                status.get("champion"), status.get("champion_provenance")
            ),
        ),
    ]
    if status.get("strategy_revision"):
        rows.append(("Strategy", f"Revision {status['strategy_revision']}"))
    if status.get("search_id") is not None:
        rows.append(
            (
                "Candidate search",
                f"Search {status['search_id']} · attempt "
                f"{status.get('search_attempt') or 0}/6",
            )
        )
    if status.get("experiment_id") is not None:
        rows.append(("Latest experiment", f"Expt #{status['experiment_id']}"))
    latest = status.get("last_result")
    if isinstance(latest, dict):
        from .results import normalize_result_statuses

        latest = normalize_result_statuses(latest)
        comparisons = latest.get("evaluation", {}).get("comparisons", [])
        final = comparisons[-1] if comparisons else None
        evidence = (
            f" · effect {_short_number(final.get('effect_estimate'))}"
            f" · lower {_short_number(final.get('one_sided_lower_bound'))}"
            if isinstance(final, dict)
            else (
                "\nNo score · "
                + safe_terminal_text(latest["failure_detail"], limit=100)
                if latest.get("failure_detail")
                else ""
            )
        )
        rows.append(
            (
                "Latest result",
                f"Expt #{latest['experiment_id']} · {latest['decision']} · "
                f"{latest['operational_status']} / {latest['scientific_status']}"
                f"{evidence}\n"
                + safe_terminal_text(latest["hypothesis"], limit=180),
            )
        )
    completed = status.get("completed_experiments", 0)
    maximum = status.get("max_experiments")
    if maximum is None:
        rows.append(("Experiment limit", "Unlimited"))
    else:
        suffix = " · reached" if status["state"] == "LIMIT_REACHED" else ""
        rows.append(("Experiment limit", f"{completed}/{maximum}{suffix}"))
    if status.get("provisional"):
        rows.append(("Suspect test", "Pending for the provisional candidate"))
    if status.get("stop_requested"):
        rows.append(("Stop", "Safe stop requested"))
    log_path = status.get("log_path") or payload.get("log_path") or "—"
    rows.append(("Logs", safe_terminal_text(log_path)))
    return tabulate(
        rows,
        headers=("Status", "Value"),
        tablefmt="simple_grid",
        disable_numparse=True,
        maxcolwidths=(18, 115),
    )


def _report_table(report: dict[str, Any]) -> str:
    from tabulate import tabulate

    from .dossier import safe_terminal_text

    rows = []
    for result in report["results"]:
        from .results import normalize_result_statuses

        result = normalize_result_statuses(result)
        comparisons = result.get("evaluation", {}).get("comparisons", [])
        final = comparisons[-1] if comparisons else None
        evidence = (
            f"{_short_number(final.get('effect_estimate'))}\n"
            f"LB {_short_number(final.get('one_sided_lower_bound'))}"
            if isinstance(final, dict)
            else (
                "No score\n"
                + safe_terminal_text(result["failure_detail"], limit=80)
                if result.get("failure_detail")
                else "—"
            )
        )
        rows.append(
            (
                f"#{result['experiment_id']}",
                result["decision"],
                f"{result['operational_status']}\n{result['scientific_status']}",
                evidence,
                safe_terminal_text(result["hypothesis"]),
                f"{result['experiment_id']:06d}",
            )
        )
    return tabulate(
        rows,
        headers=("Expt", "Decision", "Outcome", "Evidence", "Hypothesis", "Dossier"),
        tablefmt="simple_grid",
        disable_numparse=True,
        maxcolwidths=(4, 8, 16, 25, 60, 8),
    )


def _approval_table(payload: dict[str, Any]) -> str:
    from tabulate import tabulate

    from .dossier import safe_terminal_text

    summary = payload["approval_summary"]
    telemetry: list[str] = []
    for label, direction in (
        ("Higher is better", "higher"),
        ("Lower is better", "lower"),
        ("Diagnostic", "contextual"),
    ):
        metrics = summary["telemetry"][direction]
        if metrics:
            telemetry.append(label + ":")
            telemetry.extend(
                f"• {safe_terminal_text(metric['name'])} — "
                f"{safe_terminal_text(metric['description'])}"
                for metric in metrics
            )
    rows = [
        ("Method", safe_terminal_text(summary.get("method", "serial-v1"))),
        ("Models", safe_terminal_text(summary["models"])),
        ("Backends", safe_terminal_text(summary.get("backends", "codex-cli-v1 (verified)"))),
        ("Environment", safe_terminal_text(summary["environment"])),
        (
            "Editable paths",
            "\n".join(
                f"• {safe_terminal_text(path)}" for path in summary["editable_paths"]
            ),
        ),
        ("Trial seeds", safe_terminal_text(summary["trial_seeds"])),
        ("Trial count", safe_terminal_text(summary["trial_count"])),
        ("Success criterion", safe_terminal_text(summary["success_criterion"])),
        ("Telemetry", "\n".join(telemetry) if telemetry else "None declared"),
        ("Variance risks", safe_terminal_text(summary["variance_risks"])),
        ("Approval token", safe_terminal_text(payload["approval"]["confirmation_token"])),
        ("Approval command", safe_terminal_text(payload["next_command"])),
    ]
    if summary.get("candidate_review"):
        rows.insert(3, ("Policy guard", safe_terminal_text(summary["candidate_review"])))
    if summary.get("evaluator_pattern"):
        rows.insert(
            4,
            ("Evaluator pattern", safe_terminal_text(summary["evaluator_pattern"])),
        )
    return tabulate(
        rows,
        headers=("Approval item", "Value"),
        tablefmt="simple_grid",
        disable_numparse=True,
        maxcolwidths=(18, 115),
    )


def _setup_proposal_table(proposal: Sequence[dict[str, Any]]) -> str:
    from tabulate import tabulate

    from .dossier import safe_terminal_text

    rows = [
        (
            item["id"].replace("_", " ").title(),
            safe_terminal_text(
                item.get("resolved_answer", item["proposed_answer"]), limit=170
            ),
            item.get("source", "repository"),
        )
        for item in proposal
    ]
    return tabulate(
        rows,
        headers=("Setup item", "Proposal", "Source"),
        tablefmt="simple_grid",
        disable_numparse=True,
        maxcolwidths=(22, 90, 12),
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
        failure = payload.get("failure")
        if isinstance(failure, dict) and failure.get("retryable"):
            used = failure["retries_used"]
            maximum = failure["max_retries"]
            if maximum:
                print(f"Retries exhausted: {used}/{maximum}.", file=sys.stderr)
            else:
                print(
                    "Retryable: use arctl run --retries N "
                    "[--retry-delay SECONDS].",
                    file=sys.stderr,
                )
        if payload["evidence_valid"] is not None:
            print(
                "Saved evidence: "
                + ("valid and preserved." if payload["evidence_valid"] else "invalid."),
                file=sys.stderr,
            )
        if payload["log_path"] is not None:
            print(f"Details: {payload['log_path']}", file=sys.stderr)
        if (
            isinstance(payload.get("next_command"), str)
            and " setup" in payload["next_command"]
        ):
            print("Retry: " + payload["next_command"], file=sys.stderr)
    elif "status" in payload:
        status = payload["status"]
        print(_status_table(payload))
        warning = _calibration_warning(status.get("calibration_summary"))
        if warning is not None:
            print("\n" + warning)
    elif state == "REPORT":
        report = payload["report"]
        print(
            f"Task {payload['task_id']} · "
            f"{report['completed_experiments']} completed experiment(s)"
        )
        print(
            textwrap.fill(
                "Dossier root: " + report["dossier_root"],
                width=140,
                subsequent_indent="  ",
            )
        )
        if report["results"]:
            print(_report_table(report))
        else:
            print("No completed experiments.")
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
            if result.get("failure_detail"):
                print(
                    "No score: "
                    + safe_terminal_text(result["failure_detail"], limit=180)
                )
            print(f"Candidate: {result['candidate'][:12]}")
            print(
                "Champion after: "
                + _champion_display(
                    result["champion_after"],
                    payload.get("champion_after_provenance"),
                ).replace("\n", "\n  ")
            )
            print(f"Dossier: {payload['dossier_path']}")
        if show_artifacts:
            print("\nSafe artifact inventory:")
            for artifact in payload["artifacts"]:
                print(f"- [{artifact['visibility']}] {artifact['path']}")
        warning = _calibration_warning(payload.get("calibration_summary"))
        if warning is not None:
            print(warning)
    elif state == "RUN_COMPLETE" or (
        state == "LIMIT_REACHED" and "status" not in payload
    ):
        results = payload.get("results", [])
        accepted = sum(result["decision"] == "ACCEPT" for result in results)
        if results:
            print(
                f"Done: {len(results)} tested · {accepted} promoted · "
                f"{len(results) - accepted} not promoted"
            )
        if state == "LIMIT_REACHED":
            limit = payload["experiment_limit"]
            print(
                f"Experiment limit reached: {limit['completed']}/{limit['maximum']}."
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
    elif state == "SETUP_ANSWERS_REQUIRED":
        print(payload["message"])
        print(_setup_proposal_table(payload["proposal"]))
        for item in payload["open_questions"]:
            print(f"\n{item['id']}: {safe_terminal_text(item['prompt'])}")
            if item["proposed_answer"] is not None:
                print(
                    "  Proposed: "
                    + safe_terminal_text(item["proposed_answer"], limit=240)
                )
    elif state in {"READY_FOR_SETUP_ACCEPTANCE", "REVIEW_FAILED"}:
        from tabulate import tabulate

        readiness = payload.get("readiness") or {}
        rows = [
            (name.replace("_", " ").title(), value)
            for name, value in readiness.items()
            if name
            not in {"schema_version", "findings", "tree_hashes", "acceptance_token"}
        ]
        print(payload["message"])
        print(tabulate(rows, headers=("Setup item", "State"), tablefmt="simple_grid"))
        for finding in readiness.get("findings", []):
            print("- " + safe_terminal_text(finding.get("message", ""), limit=180))
        if state == "READY_FOR_SETUP_ACCEPTANCE":
            print(f"Acceptance token: {payload['acceptance_token']}")
            print(f"Accept command: {payload['next_command']}")
    elif state in {"SETUP_DISCOVERY_REQUIRED", "READY_FOR_APPROVAL"}:
        print(payload["message"])
    elif state == "SETUP_STATUS":
        from tabulate import tabulate

        rows = [("Stage", payload["setup_state"])]
        readiness = payload.get("readiness") or {}
        rows.extend(
            (name.replace("_", " ").title(), value)
            for name, value in readiness.items()
            if name not in {"schema_version", "findings", "tree_hashes", "acceptance_token"}
        )
        print(payload["message"])
        print(tabulate(rows, headers=("Setup item", "State"), tablefmt="simple_grid"))
    elif state == "TASK_DRAFT":
        print(payload["message"])
        for artifact in payload["artifacts"]:
            print(f"Edit: {artifact['path']}")
    elif state == "APPROVAL_REQUIRED":
        print(payload["message"])
        print(_approval_table(payload))
        print(
            "Note: approval trusts the evaluator's mathematics, provides no "
            "search-wide false-positive guarantee, and requires human confirmation."
        )
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
                if event.get("attempts") is None:
                    self._start(f"research planning · pass {event['attempt']}")
                else:
                    self._start(
                        f"candidate search · attempt "
                        f"{event['attempt']}/{event['attempts']}"
                    )
            elif kind == "planning":
                self._finish("complete" if event["selected"] else "exhausted")
                if event["selected"]:
                    self._start("implementation")
            elif kind == "compute_probe":
                report = event["report"]
                if report["risk"] == "likely_over_budget":
                    projected = self._duration(report["projected_seconds"])
                    budget = self._duration(report["timeout_seconds"])
                    self._line(
                        f"    Compute warning: projected {projected} for a "
                        f"{budget} evaluation budget (advisory)."
                    )
            elif kind == "retry":
                self._finish("failed")
                delay = self._duration(event["delay_seconds"])
                self._line(
                    f"  ↻ {event['stage']} · {event['category']} · retry "
                    f"{event['attempt']}/{event['attempts']} in {delay}"
                )
            elif kind == "search_miss":
                self._finish("failed")
                self._line(
                    "    Miss: "
                    + safe_terminal_text(event["message"], limit=120)
                )
            elif kind == "research":
                self._start("RESEARCHING")
            elif kind == "candidate_review":
                self._finish()
                self._start(
                    f"policy review · round {event['round']}/{event['rounds']}"
                )
            elif kind == "candidate_repair":
                self._finish("failed")
                self._start(
                    f"policy repair · attempt {event['attempt']}/{event['attempts']}"
                )
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
                self._line(
                    "    Reason: "
                    + safe_terminal_text(event["message"], limit=180)
                )
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


class _SetupProgressView(_ProgressView):
    """Render the pre-approval setup lifecycle with live elapsed time."""

    def __call__(self, event: dict[str, Any]) -> None:
        with self._lock:
            if event["status"] == "started":
                self._start(event["stage"])
            else:
                self._finish("failed" if event["status"] == "failed" else "complete")


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
            prefix += " --data " + shlex.quote(str(data_root))
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


def _init(
    repo_argument: Path | None,
    new_repo_argument: Path | None,
    workspace_argument: Path | None,
    task_id: str | None,
    data_argument: Path | None,
) -> dict[str, Any]:
    from .setup import initialize_setup

    if (repo_argument is None) == (new_repo_argument is None):
        raise StateError("init requires exactly one of --repo or --new-repo")
    if new_repo_argument is not None and workspace_argument is not None:
        raise StateError("--workspace cannot be combined with --new-repo")
    if (
        data_argument is not None
        and repo_argument is not None
        and new_repo_argument is None
        and workspace_argument is None
    ):
        repo = repo_argument.resolve()
        if not (repo / ".git").exists():
            raise StateError(f"target is not a Git worktree: {repo}")
        identifier = task_id or repo.name
        try:
            validate_task_id(identifier)
        except ArctlError as error:
            raise StateError("task ID contains unsafe path or Git-ref characters") from error
        task_directory = data_argument.resolve() / "tasks" / identifier
        task_file = task_directory / "task.yaml"
        if task_file.exists():
            raise StateError(f"task already exists: {identifier}")
        task_directory.mkdir(parents=True, exist_ok=False)
        atomic_write_text(
            task_file,
            _TASK_TEMPLATE.format(
                task_id=json.dumps(identifier),
                repo=json.dumps(str(repo)),
                environment_commit=resolve_commit(repo, "HEAD"),
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
    if new_repo_argument is not None:
        workspace = new_repo_argument.resolve()
        repo = None
        identifier = task_id or workspace.name
        new_repo = True
    else:
        assert repo_argument is not None
        repo = repo_argument.resolve()
        workspace = (
            workspace_argument.resolve()
            if workspace_argument is not None
            else repo.parent / f"{repo.name}-research"
        )
        identifier = task_id or repo.name
        new_repo = False
    try:
        validate_task_id(identifier)
    except ArctlError as error:
        raise StateError("task ID contains unsafe path or Git-ref characters") from error
    data_root = data_argument.resolve() if data_argument is not None else workspace / ".arctl-data"
    record = initialize_setup(
        data_root=data_root,
        workspace=workspace,
        repo=repo,
        new_repo=new_repo,
        task_id=identifier,
    )
    return _payload(
        success=True,
        state="SETUP_DISCOVERY_REQUIRED",
        task_id=identifier,
        action_required=True,
        allowed_actions=("setup",),
        next_command=f"arctl setup {identifier}",
        message=(
            f"Created Python research workspace {record['workspace']}. "
            f"Review {record['setup_brief']}, then run setup."
        ),
        artifacts=(
            {"kind": "workspace", "path": record["workspace"]},
            {"kind": "setup_brief", "path": record["setup_brief"]},
            {"kind": "setup", "path": str(data_root / "tasks" / identifier)},
        ),
    )


def _setup(
    *,
    data_root: Path,
    task_id: str | None,
    answers_path: Path | None,
    offline: bool,
    acceptance: str | None,
    interactive: bool,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    from .setup import (
        accept_setup,
        brief_changed,
        build_setup,
        discover_setup,
        load_setup,
        resolve_fields,
        save_answers,
        setup_presentation,
    )

    directory, record = load_setup(data_root, task_id)
    identifier = record["task_id"]
    state = record["state"]
    discovery: dict[str, Any] | None = None
    discovery_path = directory / "setup" / "discovery.public.json"
    if state in {
        "ANSWERS_REQUIRED",
        "BUILD_REQUIRED",
        "REVIEW_FAILED",
        "READY_FOR_SETUP_ACCEPTANCE",
    } and discovery_path.is_file():
        discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
        if brief_changed(record, discovery):
            record["state"] = "DISCOVERY_REQUIRED"
            atomic_write_json(directory / "setup.json", record)
            state = "DISCOVERY_REQUIRED"
    if state == "DISCOVERY_REQUIRED":
        discovery = discover_setup(
            directory,
            record,
            offline=offline,
            progress=progress,
        )
        record = json.loads((directory / "setup.json").read_text())
        state = record["state"]
    presentation = setup_presentation(discovery) if discovery is not None else None
    if state == "ANSWERS_REQUIRED" and answers_path is not None:
        if progress is not None:
            progress({"stage": "requirement confirmation", "status": "started"})
        try:
            raw_answers = json.loads(answers_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("setup answers file is missing or invalid") from error
        if not isinstance(raw_answers, dict):
            raise StateError("setup answers file must contain one answers object")
        if set(raw_answers) <= {"answers", "overrides"}:
            answers = raw_answers.get("answers", {})
            overrides = raw_answers.get("overrides", {})
        else:
            answers = raw_answers
            overrides = {}
        if not isinstance(answers, dict) or not isinstance(overrides, dict):
            raise StateError("setup answers and overrides must be JSON objects")
        save_answers(directory, record, answers, overrides)
        if progress is not None:
            progress({"stage": "requirement confirmation", "status": "completed"})
        record = json.loads((directory / "setup.json").read_text())
        state = record["state"]
    elif state == "ANSWERS_REQUIRED" and interactive and sys.stdin.isatty():
        if discovery is None:
            discovery = json.loads(discovery_path.read_text())
        presentation = setup_presentation(discovery)
        if progress is not None:
            progress({"stage": "requirement confirmation", "status": "started"})
            progress({"stage": "requirement confirmation", "status": "completed"})
        print("\nProposed setup")
        print(_setup_proposal_table(presentation["proposal"]))
        answers: dict[str, str] = {}
        for item in presentation["open_questions"]:
            print(f"\n{item['prompt']}")
            proposed = item["proposed_answer"]
            if proposed is not None:
                print("Proposed: " + proposed)
                entered = input("Answer [Enter accepts proposal]: ").strip()
                answers[item["id"]] = entered or proposed
            else:
                entered = ""
                while not entered:
                    entered = input("Answer [required]: ").strip()
                answers[item["id"]] = entered
        overrides: dict[str, str] = {}
        print("\nResolved setup")
        print(_setup_proposal_table(resolve_fields(presentation, answers, overrides)))
        decision = input("\nUse this setup? [Y/edit/n]: ").strip().lower()
        if decision in {"n", "no"}:
            raise StateError("setup confirmation cancelled")
        if decision in {"e", "edit"}:
            proposal = presentation["proposal"]
            while True:
                for index, item in enumerate(proposal, 1):
                    print(f"  {index}. {item['id'].replace('_', ' ')}")
                selected = input("Field number to edit [Enter finishes]: ").strip()
                if not selected:
                    break
                if not selected.isdecimal() or not 1 <= int(selected) <= len(proposal):
                    print("Choose one listed field number.")
                    continue
                item = proposal[int(selected) - 1]
                changed = input(f"{item['id']}: ").strip()
                if changed:
                    overrides[item["id"]] = changed
            print("\nResolved setup")
            print(_setup_proposal_table(resolve_fields(presentation, answers, overrides)))
            if input("\nUse this setup? [Y/n]: ").strip().lower() in {"n", "no"}:
                raise StateError("setup confirmation cancelled")
        save_answers(directory, record, answers, overrides)
        record = json.loads((directory / "setup.json").read_text())
        state = record["state"]
    if state in {"BUILD_REQUIRED", "REVIEW_FAILED"}:
        readiness = build_setup(
            directory,
            record,
            offline=offline,
            progress=progress,
        )
        record = json.loads((directory / "setup.json").read_text())
        state = record["state"]
    else:
        readiness_path = directory / "setup" / "readiness.public.json"
        readiness = (
            json.loads(readiness_path.read_text()) if readiness_path.is_file() else None
        )
    if (
        state == "READY_FOR_SETUP_ACCEPTANCE"
        and acceptance is None
        and interactive
        and sys.stdin.isatty()
    ):
        assert readiness is not None
        if input("Accept and commit this verified setup? [y/N]: ").strip().lower() in {
            "y",
            "yes",
        }:
            acceptance = readiness["acceptance_token"]
    if state == "READY_FOR_SETUP_ACCEPTANCE" and acceptance is not None:
        task = accept_setup(directory, record, acceptance)
        state = "READY_FOR_APPROVAL"
        return {
            **_payload(
                success=True,
                state=state,
                task_id=identifier,
                action_required=True,
                allowed_actions=("approve",),
                next_command=f"arctl approve {identifier}",
                message="Setup accepted; the exact generated task is ready for approval.",
                log_path=str(directory),
            ),
            "task": {"schema_version": task.schema_version, "repo": str(task.repo)},
            "readiness": readiness,
        }
    if state == "ANSWERS_REQUIRED":
        if discovery is None:
            discovery = json.loads(discovery_path.read_text())
        presentation = setup_presentation(discovery)
        return {
            **_payload(
                success=True,
                state="SETUP_ANSWERS_REQUIRED",
                task_id=identifier,
                action_required=True,
                allowed_actions=("setup",),
                next_command=f"arctl setup {identifier} --answers ANSWERS.json",
                message="Confirm or revise the cited setup requirements.",
                log_path=str(directory / "setup"),
            ),
            "proposal": presentation["proposal"],
            "open_questions": presentation["open_questions"],
            "answer_schema": {
                "type": "object",
                "properties": {
                    "answers": {
                        "type": "object",
                        "required": [
                            item["id"] for item in presentation["open_questions"]
                        ],
                    },
                    "overrides": {"type": "object"},
                },
                "required": ["answers"],
            },
        }
    if state == "READY_FOR_SETUP_ACCEPTANCE":
        assert readiness is not None
        token = readiness["acceptance_token"]
        return {
            **_payload(
                success=True,
                state=state,
                task_id=identifier,
                action_required=True,
                allowed_actions=("setup_accept",),
                next_command=f"arctl setup {identifier} --accept {token}",
                message="Generated workspace passed setup review and awaits acceptance.",
                log_path=str(directory / "setup"),
            ),
            "readiness": readiness,
            "acceptance_token": token,
        }
    return {
        **_payload(
            success=state != "REVIEW_FAILED",
            state=state,
            task_id=identifier,
            action_required=True,
            allowed_actions=("setup",),
            next_command=f"arctl setup {identifier}",
            message="Setup review found issues that must be corrected."
            if state == "REVIEW_FAILED"
            else "Setup is incomplete.",
            log_path=str(directory / "setup"),
        ),
        "readiness": readiness,
    }


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
        trial_count = (
            (
                f"Sweep {list(manifest.calibration.ladder)}; first meeting "
                f"{manifest.calibration.diagnostic_name} ≤ "
                f"{manifest.calibration.diagnostic_maximum} "
                f"{manifest.calibration.diagnostic_units}; otherwise "
                f"{manifest.calibration.ceiling}."
            )
            if located.config.trials == "auto"
            else f"{located.config.trials} paired trials."
        )
        telemetry = {
            direction: [
                {"name": name, "description": metric.description}
                for name, metric in sorted(manifest.public_telemetry.items())
                if metric.direction == direction
            ]
            for direction in ("higher", "lower", "contextual")
        }
        setup_path = located.directory / "setup.json"
        setup_record = (
            json.loads(setup_path.read_text(encoding="utf-8"))
            if setup_path.is_file()
            else {}
        )
        return {
            **_payload(
                success=True,
                state="APPROVAL_REQUIRED",
                task_id=located.config.task_id,
                action_required=True,
                allowed_actions=("approve",),
                next_command=command,
                message=f"Approval required for task {located.config.task_id}.",
            ),
            "approval": {
                "task_sha256": preview.task_hash,
                "evaluator_commit": preview.evaluator_commit,
                "manifest_sha256": preview.manifest_hash,
                "environment_sha256": dict(preview.environment_hashes),
                "method_sha256": preview.method_hash,
                "backend_approval_sha256": preview.backend_hash,
                "confirmation_token": preview.confirmation_token,
            },
            "approval_summary": {
                "method": (
                    located.config.method.profile
                    if located.config.method is not None
                    else "serial-v1"
                ),
                "models": "; ".join(
                    f"{component.title()}: "
                    + ", ".join(
                        f"{agent.name}={agent.model} {agent.reasoning_effort}"
                        for agent in located.config.method.pool(component)
                    )
                    for component in ("strategize", "plan", "execute", "reflect")
                ),
                "backends": "; ".join(
                    f"{name} ({details['certification']})"
                    for name, details in preview.backend_attestations.items()
                ),
                "editable_paths": list(located.config.editable_paths),
                "environment": ", ".join(
                    source.identifier for source in located.config.environment_sources
                ),
                "evaluator_pattern": setup_record.get("evaluator_pattern"),
                "candidate_review": (
                    f"Reviewer + {len(located.config.candidate_review.checks)} "
                    f"tripwire(s); {located.config.candidate_review.repair_attempts} repair."
                    if located.config.candidate_review is not None
                    else None
                ),
                "trial_seeds": (
                    "Hidden seeds test both champion and candidate; not reused "
                    "within this task. Evaluator mapping: "
                    + manifest.seed_to_case.rstrip(".")
                    + "."
                ),
                "trial_count": trial_count,
                "success_criterion": (
                    "Hard rules pass; lower bound > 0 — "
                    + manifest.positive_effect.rstrip(".")
                    + "."
                ),
                "telemetry": telemetry,
                "variance_risks": (
                    manifest.known_variation.rstrip(".")
                    + ". Mitigation: "
                    + (
                        ", ".join(manifest.variation_mitigations).rstrip(".")
                        or "none declared"
                    )
                    + "."
                ),
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
    from .setup import load_setup

    try:
        setup_directory, setup = load_setup(data_root, task_id)
    except StateError:
        setup_directory = None
        setup = None
    if setup is not None and not (setup_directory / "task.yaml").is_file():
        identifier = setup["task_id"]
        readiness_path = setup_directory / "setup" / "readiness.public.json"
        readiness = (
            json.loads(readiness_path.read_text(encoding="utf-8"))
            if readiness_path.is_file()
            else None
        )
        return {
            **_payload(
                success=True,
                state="SETUP_STATUS",
                task_id=identifier,
                action_required=True,
                allowed_actions=("setup",),
                next_command=f"arctl setup {identifier}",
                message=f"Task {identifier} setup is {setup['state']}.",
                log_path=str(setup_directory / "setup"),
            ),
            "setup_state": setup["state"],
            "readiness": readiness,
        }

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
        "PLANNING_FAILED",
        "IMPLEMENTATION_FAILED",
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
    elif status["state"] == "LIMIT_REACHED":
        next_command = f"arctl report {identifier}"
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
                else (
                    ("status", "report", "history")
                    if status["state"] == "LIMIT_REACHED"
                    else ("run", "status", "stop", "report", "history")
                )
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
    payload["champion_after_provenance"] = inspection[
        "champion_after_provenance"
    ]
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
    retries: int = 0,
    retry_delay: float = 60.0,
    preflight: bool = True,
    progress=None,
) -> dict[str, Any]:
    from .downstream import RetryPolicy
    from .doctor import run_doctor
    from .runner import RunOutcome, run_task

    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise StateError("retries must be a non-negative integer")
    if max_experiments is not None and (
        isinstance(max_experiments, bool)
        or not isinstance(max_experiments, int)
        or max_experiments <= 0
    ):
        raise StateError("max experiments must be a positive integer")
    if (
        isinstance(retry_delay, bool)
        or not isinstance(retry_delay, (int, float))
        or not math.isfinite(retry_delay)
        or retry_delay < 0
    ):
        raise StateError("retry delay must be non-negative")
    task = _located(data_root, task_id)
    if preflight:
        checks = run_doctor()
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise StateError(
                "runtime or sandbox preflight failed: " + ", ".join(failed)
            )
    initial_results = sorted(
        (task.directory / "experiments").glob("[0-9]" * 6)
    ) if (task.directory / "experiments").is_dir() else []
    initial_completed = sum(
        (directory / "published").is_file() for directory in initial_results
    )
    initial_ids = {int(directory.name) for directory in initial_results}
    policy = RetryPolicy(
        retries,
        retry_delay,
        progress=progress,
        stop_path=task.directory / "stop.requested",
    )

    def tracked_progress(event: dict[str, Any]) -> None:
        if event["event"] in {
            "strategy",
            "planning",
            "candidate_review",
            "candidate",
            "public_checks_complete",
            "reflection_complete",
        }:
            policy.succeeded()
        if progress is not None:
            progress(event)

    with TaskLock(task.directory / "lock"):
        while True:
            completed = sum(
                (directory / "published").is_file()
                for directory in (task.directory / "experiments").glob("[0-9]" * 6)
            )
            if (
                task.config.max_experiments is not None
                and completed >= task.config.max_experiments
            ):
                outcome = RunOutcome((), False, limit_reached=True)
                remaining = 0
                break
            remaining = (
                None
                if max_experiments is None
                else max(max_experiments - (completed - initial_completed), 0)
            )
            if remaining == 0:
                break
            try:
                outcome = run_task(
                    task,
                    max_experiments=remaining,
                    progress=tracked_progress,
                )
            except TransientDownstreamError as error:
                policy.wait(error)
                continue
            break
    completed_results: dict[int, dict[str, Any]] = {}
    for directory in sorted((task.directory / "experiments").glob("[0-9]" * 6)):
        if int(directory.name) in initial_ids:
            continue
        result_path = directory / "result.public.json"
        if (directory / "published").is_file() and result_path.is_file():
            value = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                completed_results[value["experiment_id"]] = value
    if remaining != 0:
        for value in outcome.results:
            completed_results[value["experiment_id"]] = value
    results = tuple(completed_results[key] for key in sorted(completed_results))
    if remaining == 0 and not outcome.limit_reached:
        outcome = RunOutcome(results, False)
    identifier = task.config.task_id
    last = results[-1] if results else None
    state = (
        "STOPPED"
        if outcome.stopped
        else "REFLECTION_FAILED"
        if outcome.reflection_failed
        else "SEARCH_STALLED"
        if outcome.stalled
        else "LIMIT_REACHED"
        if outcome.limit_reached
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
            (
                f"Task {identifier} stopped safely after {len(results)} "
                f"experiment{'s' if len(results) != 1 else ''} in this run."
                if results
                else f"Task {identifier} stopped safely during candidate search; "
                "no experiments completed in this run."
            )
            if outcome.stopped
            else (
                f"Post-trial reflection failed for {identifier}; valid statistical "
                "evidence was preserved and no further research was started."
                + (
                    f" Reason: {outcome.reflection_error}"
                    if outcome.reflection_error
                    else ""
                )
                if outcome.reflection_failed
                else
                f"Candidate search for {identifier} stalled after six attempts; "
                "the exploration history was preserved."
                if outcome.stalled
                else
                f"Task {identifier} reached its approved experiment limit "
                f"({task.config.max_experiments}/{task.config.max_experiments})."
                if outcome.limit_reached
                else f"Task {identifier} completed {len(results)} experiments."
            )
        ),
        evidence_valid=True if results else None,
        log_path=str(task.directory),
    )
    if last is not None:
        payload["experiment_id"] = last["experiment_id"]
    payload["results"] = results
    if outcome.limit_reached:
        payload["experiment_limit"] = {
            "completed": task.config.max_experiments,
            "maximum": task.config.max_experiments,
        }
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
    formatter = lambda prog: argparse.RawDescriptionHelpFormatter(  # noqa: E731
        prog, max_help_position=30, width=100
    )
    parser = argparse.ArgumentParser(
        prog="arctl",
        formatter_class=formatter,
        description=(
            "Run faithful, statistically cautious AutoResearch loops against local Git repositories.\n"
            "\n"
            "arctl separates untrusted research from an approval-locked evaluator, preserves every\n"
            "official comparison, and promotes candidates only through the evaluator's fixed rule."
        ),
        epilog=(
            "Typical workflow:\n"
            "  arctl doctor\n"
            "  arctl init --repo /path/to/subject\n"
            "  arctl setup TASK\n"
            "  arctl approve TASK\n"
            "  arctl approve TASK --confirm TOKEN\n"
            "  arctl run TASK --max-experiments 3\n"
            "  arctl status TASK\n"
            "\n"
            "Complete command forms:\n"
            "  arctl doctor [--json]\n"
            "  arctl init (--repo PATH | --new-repo PATH) [--workspace PATH]\n"
            "             [--task-id TASK] [--json]\n"
            "  arctl setup [TASK] [--answers FILE] [--offline]\n"
            "                     [--accept TOKEN] [--json]\n"
            "  arctl approve [TASK] [--confirm TOKEN] [--json]\n"
            "  arctl run [TASK] [--max-experiments N] [--retries N]\n"
            "                    [--retry-delay SECONDS] [--json]\n"
            "  arctl status [TASK] [--json]\n"
            "  arctl stop [TASK] [--json]\n"
            "  arctl report [TASK] [--json]\n"
            "  arctl history [TASK] [--query TEXT] [--path GLOB]\n"
            "                        [--decision VALUE] [--json]\n"
            "  arctl inspect [TASK] [EXPERIMENT] [--artifacts] [--json]\n"
            "\n"
            "Task IDs may be omitted when the current directory identifies exactly one task.\n"
            "Use --json on any command for the stable AI-orchestration response. Use --debug to\n"
            "show controller tracebacks. Run `arctl COMMAND -h` for command-specific details."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show controller tracebacks instead of concise failure reports",
    )
    parser.add_argument("--data", type=Path, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(
        dest="command", required=True, title="commands", metavar="COMMAND"
    )

    def command(name: str, summary: str, description: str, epilog: str | None = None):
        return subparsers.add_parser(
            name,
            help=summary,
            description=description,
            epilog=epilog,
            formatter_class=formatter,
        )

    def json_option(child) -> None:
        child.add_argument(
            "--json",
            action="store_true",
            help="emit one stable machine-readable JSON object",
        )

    def task_argument(child) -> None:
        child.add_argument(
            "task_id",
            nargs="?",
            metavar="TASK",
            help="task ID; omit when the current repository identifies exactly one task",
        )

    doctor = command(
        "doctor",
        "check Git, Codex, sandbox, runtime, network, and cleanup support",
        "Run non-destructive installation and sandbox capability checks required by arctl.",
        "Run this after installation or when a sandbox/runtime preflight fails.",
    )
    json_option(doctor)

    init = command(
        "init",
        "create a guided Python research workspace",
        "Create visible setup storage for an existing or new Python subject repository.",
        "Example:\n  arctl init --repo . --task-id routing-policy",
    )
    json_option(init)
    init.add_argument(
        "--repo",
        type=Path,
        metavar="PATH",
        help="existing clean local Git worktree containing the policy to improve",
    )
    init.add_argument(
        "--new-repo",
        type=Path,
        metavar="PATH",
        help="create a visible workspace and new subject repository at PATH",
    )
    init.add_argument(
        "--workspace",
        type=Path,
        metavar="PATH",
        help="workspace path; defaults to a visible sibling of an existing repo",
    )
    init.add_argument(
        "--task-id",
        metavar="TASK",
        help="task ID; defaults to the repository directory name",
    )

    setup = command(
        "setup",
        "discover, build, review, and accept a Python task workspace",
        "Run resumable pre-approval setup. Discovery is read-only; generated code and evaluator\n"
        "remain drafts until explicit setup acceptance and normal task approval.",
        "AI operators should use --json, resolve returned clarification IDs with --answers,\n"
        "and obtain permission before using --accept.",
    )
    json_option(setup)
    task_argument(setup)
    setup.add_argument(
        "--answers",
        type=Path,
        metavar="FILE",
        help="JSON answers for open clarification IDs plus optional canonical overrides",
    )
    setup.add_argument(
        "--offline",
        action="store_true",
        help="disable setup-agent internet access and require cached uv dependencies",
    )
    setup.add_argument(
        "--accept",
        metavar="TOKEN",
        help="accept the verified setup trees and create their local Git commits",
    )

    approve = command(
        "approve",
        "preview or confirm the task, evaluator, environment, and champion lock",
        "Without --confirm, validate the draft and print the human approval table and token.\n"
        "With --confirm, lock the exact task, evaluator commit, environment sources, and initial champion.",
        "Approval is a human trust boundary. An AI operator must not confirm without explicit permission.",
    )
    json_option(approve)
    task_argument(approve)
    approve.add_argument(
        "--confirm",
        metavar="TOKEN",
        help="confirmation token printed by the approval preview",
    )

    run = command(
        "run",
        "calibrate if needed, search for candidates, and run official experiments",
        "Resume TASK safely and run a bounded number of new experiments. Existing process records,\n"
        "comparisons, reflections, and failed candidate reviews are recovered before new work starts.",
        "Retries apply only to recognized transient Codex and pre-trial public-process failures.\n"
        "Started calibration and official comparison commands are never retried.\n"
        "\n"
        "Example:\n  arctl run TASK --max-experiments 3 --retries 2 --retry-delay 60",
    )
    json_option(run)
    task_argument(run)
    run.add_argument(
        "--max-experiments",
        type=int,
        metavar="N",
        help="maximum experiments for this invocation; cannot exceed the approved task limit",
    )
    run.add_argument(
        "--retries",
        type=int,
        default=0,
        metavar="N",
        help="additional consecutive transient attempts (default: 0)",
    )
    run.add_argument(
        "--retry-delay",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="fixed interruptible delay between transient retries (default: 60)",
    )

    status = command(
        "status",
        "show the task's current controller state and resumability",
        "Show approval, calibration, frozen trial count, champion, active work, latest result,\n"
        "search progress, stop state, experiment-limit state, and the relevant log path.",
    )
    json_option(status)
    task_argument(status)

    stop = command(
        "stop",
        "request a safe idempotent stop",
        "Create TASK's persistent stop request. The active managed process is terminated at a safe\n"
        "boundary; reserved evidence is preserved and never silently redrawn.",
        "Calling stop repeatedly is safe. Ctrl-C during `arctl run` requests the same stop.",
    )
    json_option(stop)
    task_argument(stop)

    report = command(
        "report",
        "list completed experiments and public dossier paths",
        "Show the completed experiment history, decisions, aggregate effects, and immutable public\n"
        "Markdown dossier paths. Private cases, seeds, and raw outputs are not disclosed.",
    )
    json_option(report)
    task_argument(report)

    history = command(
        "history",
        "search the public strategy and experiment exploration ledger",
        "Search immutable public exploration entries used by later candidate executors.",
        "Filters combine with AND. Text matching is case-insensitive; --path accepts shell-style globs.\n"
        "Example:\n  arctl history TASK --query lookahead --decision REJECT",
    )
    json_option(history)
    task_argument(history)
    history.add_argument(
        "--query",
        metavar="TEXT",
        help="require all words in the entry's searchable public text",
    )
    history.add_argument(
        "--path",
        metavar="GLOB",
        help="match at least one candidate changed path",
    )
    history.add_argument(
        "--decision",
        metavar="VALUE",
        help="match an exact decision such as ACCEPT, REJECT, ARCHIVE, or INVALID",
    )

    inspect = command(
        "inspect",
        "inspect one experiment and its safe artifacts",
        "Show one experiment's hypothesis, decision, aggregate comparisons, commits, and public dossier.\n"
        "When TASK is inferable, a lone numeric positional argument is treated as EXPERIMENT.",
        "Examples:\n  arctl inspect TASK 4\n  arctl inspect 4 --artifacts",
    )
    json_option(inspect)
    task_argument(inspect)
    inspect.add_argument(
        "experiment_id",
        nargs="?",
        type=int,
        metavar="EXPERIMENT",
        help="positive experiment number; defaults to the latest experiment",
    )
    inspect.add_argument(
        "--artifacts",
        action="store_true",
        help="include the safe public/private artifact inventory",
    )
    return parser


def render_cli_reference() -> str:
    """Render the checked-in Markdown mirror of every public help screen."""
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    sections = [
        "# arctl command reference\n\n"
        "This file mirrors the built-in CLI help. Regenerate it with "
        "`.venv/bin/python tools/generate_cli_reference.py`.\n\n"
        "## `arctl -h`\n\n```text\n"
        + parser.format_help().rstrip()
        + "\n```\n"
    ]
    for name, child in subparsers.choices.items():
        sections.append(
            f"\n## `arctl {name} -h`\n\n```text\n"
            + child.format_help().rstrip()
            + "\n```\n"
        )
    return "".join(sections)


def main(argv: Sequence[str] | None = None) -> int:
    program = _invoked_program(argv)
    arguments = build_parser().parse_args(argv)
    progress_view = (
        _ProgressView()
        if arguments.command == "run" and not arguments.json
        else None
    )
    setup_progress = (
        _SetupProgressView()
        if arguments.command == "setup" and not arguments.json
        else None
    )
    setup_events: list[dict[str, Any]] = []
    try:
        if arguments.command == "doctor":
            payload = _doctor()
        elif arguments.command == "init":
            payload = _init(
                arguments.repo,
                arguments.new_repo,
                arguments.workspace,
                arguments.task_id,
                arguments.data,
            )
        elif arguments.command == "setup":
            payload = _setup(
                data_root=_data_root(arguments.data),
                task_id=arguments.task_id,
                answers_path=arguments.answers,
                offline=arguments.offline,
                acceptance=arguments.accept,
                interactive=not arguments.json,
                progress=(
                    setup_progress
                    if setup_progress is not None
                    else lambda event: setup_events.append(dict(event))
                ),
            )
            if arguments.json:
                payload["progress_events"] = setup_events
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
                retries=arguments.retries,
                retry_delay=arguments.retry_delay,
                progress=progress_view,
            )
        else:
            raise StateError(f"unsupported command: {arguments.command}")
    except KeyboardInterrupt:
        if arguments.command == "setup":
            identifier = arguments.task_id
            payload = _payload(
                success=True,
                state="SETUP_STOPPED",
                task_id=identifier,
                action_required=True,
                allowed_actions=("setup", "status"),
                next_command=f"arctl setup {identifier}" if identifier else "arctl setup",
                message="Setup stopped safely; no unanswered requirements were saved.",
            )
        elif arguments.command != "run":
            raise
        else:
            from .operations import request_stop

            data_root = _data_root(arguments.data)
            task = _located(data_root, arguments.task_id)
            request_stop(task)
            payload = _run(
                data_root,
                arguments.task_id,
                arguments.max_experiments,
                retries=arguments.retries,
                retry_delay=arguments.retry_delay,
                preflight=False,
                progress=progress_view,
            )
    except ArctlError as error:
        if arguments.debug:
            if progress_view is not None:
                progress_view.close(failed=True)
            if setup_progress is not None:
                setup_progress.close(failed=True)
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
        elif arguments.command == "setup":
            next_command = f"arctl setup {identifier}" if identifier else "arctl setup"
        else:
            next_command = (
                f"arctl status {identifier}" if identifier else "arctl status"
            )
        transient = (
            error if isinstance(error, TransientDownstreamError) else None
        )
        payload = _payload(
            success=False,
            state="ERROR",
            task_id=identifier,
            action_required=True,
            allowed_actions=(
                ("status", "run")
                if transient
                else (
                    ("setup", "status")
                    if arguments.command == "setup"
                    else ("status",)
                )
            ),
            next_command=(
                f"arctl run {identifier} --max-experiments 1"
                if transient and identifier
                else next_command
            ),
            message=(
                f"Failed: {transient.stage} · {transient.detail}."
                if transient
                else f"Failed: {error}."
            ),
            evidence_valid=True,
            can_continue=transient is not None or arguments.command == "setup",
            log_path=transient.artifact_path if transient else log_path,
        )
        if transient is not None:
            payload["failure"] = {
                "stage": transient.stage,
                "category": transient.category,
                "detail": transient.detail,
                "retryable": True,
                "retries_used": transient.retries_used,
                "max_retries": transient.max_retries,
                "artifact_path": transient.artifact_path,
            }
    if progress_view is not None:
        progress_view.close(failed=not payload["success"])
    if setup_progress is not None:
        setup_progress.close(failed=not payload["success"])
    if arguments.command == "setup" and arguments.json:
        payload["progress_events"] = setup_events
    _rewrite_next_command(payload, program, arguments.data)
    _emit(
        payload,
        as_json=arguments.json,
        show_artifacts=bool(
            arguments.command == "inspect" and arguments.artifacts
        ),
    )
    return 0 if payload["success"] else 1
