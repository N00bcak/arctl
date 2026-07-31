"""Public-only human-readable views of completed experiments."""

from __future__ import annotations

import html
import json
import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .errors import StateError
from .git import candidate_changed_paths, candidate_diff
from .models import TaskConfig
from .storage import atomic_write_text
from .trials import load_trial_count_record

_CONTROL = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]"
)
_MARKDOWN = re.compile(r"([\\`*\[\]<>!])")


def safe_text(value: object, *, limit: int | None = None) -> str:
    """Render untrusted text without terminal controls or active Markdown."""
    text = safe_terminal_text(value, limit=limit)
    return _MARKDOWN.sub(r"\\\1", html.escape(text, quote=False))


def safe_terminal_text(value: object, *, limit: int | None = None) -> str:
    """Render untrusted text as one inert terminal line."""
    text = _CONTROL.sub(" ", str(value)).replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())
    if limit is not None and len(text) > limit:
        text = text[: max(limit - 1, 0)].rstrip() + "…"
    return text


def _code(value: object) -> str:
    return f"<code>{html.escape(safe_terminal_text(value), quote=False)}</code>"


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"{label} is missing or invalid") from error
    if not isinstance(value, dict):
        raise StateError(f"{label} must contain one JSON object")
    return value


def _comparison_lines(result: dict[str, Any]) -> list[str]:
    comparisons = result.get("evaluation", {}).get("comparisons", [])
    lines = [
        "| Batch | Trials | Effect | One-sided lower bound | Suspect reason |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for comparison in comparisons:
        lines.append(
            "| "
            + " | ".join(
                (
                    safe_text(comparison.get("kind", "unknown")),
                    str(comparison.get("trials", "—")),
                    str(comparison.get("effect_estimate", "—")),
                    str(comparison.get("one_sided_lower_bound", "—")),
                    safe_text(comparison.get("suspect_test_reason") or "—"),
                )
            )
            + " |"
        )
    return lines


def _public_check_lines(
    task: TaskConfig,
    experiment: Path,
    result: dict[str, Any],
) -> list[str]:
    lines: list[str] = []
    for index, command in enumerate(task.public_checks, start=1):
        process_result = (
            experiment / "process" / f"public-check-{index:04d}" / "result.json"
        )
        if process_result.is_file():
            saved = _read_object(process_result, f"public check {index} result")
            outcome = "PASS" if saved.get("return_code") == 0 else "FAIL"
        else:
            outcome = "NOT RUN"
        lines.append(f"- {_code(shlex.join(command))} — **{outcome}**")
    if not lines:
        lines.append(
            f"- Public constraints: **{safe_text(result.get('constraints', {}).get('tests', 'unknown'))}**"
        )
    return lines


def _documents(
    task_directory: Path,
    task: TaskConfig,
    experiment: Path,
    result: dict[str, Any],
) -> dict[str, str]:
    request = _read_object(experiment / "request.public.json", "public research record")
    champion = str(result["champion_before"])
    candidate = str(result["candidate"])
    changed = candidate_changed_paths(task.repo, champion, candidate)
    decision = safe_text(result["decision"])
    identifier = int(result["experiment_id"])
    dossier_note = (
        "> Derived human-readable view. Git commits and arctl JSON records remain "
        "authoritative.\n"
    )
    readme = "\n".join(
        (
            f"# Experiment {identifier}",
            "",
            dossier_note,
            "",
            f"**Hypothesis:** {safe_text(result['hypothesis'])}",
            "",
            f"**Candidate:** {_code(candidate[:12])} from champion "
            f"{_code(champion[:12])}",
            "",
            f"**Change:** {len(changed)} file(s): "
            + ", ".join(_code(path) for path in changed),
            "",
            f"**Decision:** **{decision}**",
            "",
            "- [Research rationale](research.md)",
            "- [Exact candidate change](change.diff)",
            "- [Checks and official evaluation](evaluation.md)",
            "",
        )
    )
    falsifiers = request.get("falsifiers", [])
    research = "\n".join(
        (
            f"# Research — Experiment {identifier}",
            "",
            dossier_note,
            "",
            f"## Claim\n\n{safe_text(request.get('claim', ''))}",
            "",
            f"## Mechanism\n\n{safe_text(request.get('mechanism', ''))}",
            "",
            f"## Expected effect\n\n{safe_text(request.get('expected_effect', ''))}",
            "",
            "## Expected telemetry",
            "",
            *(
                [
                    f"- **{safe_text(name)}:** {safe_text(expectation)}"
                    for name, expectation in request.get("expected_telemetry", {}).items()
                ]
                or ["- None declared."]
            ),
            "",
            "## Falsifiers",
            "",
            *[f"- {safe_text(item)}" for item in falsifiers],
            "",
        )
    )
    telemetry = result.get("telemetry", {})
    trial_record = (
        load_trial_count_record(task_directory, task)
        if (task_directory / "trial-count.json").is_file()
        else {}
    )
    calibration = trial_record.get("calibration")
    calibration_note = (
        "Calibration warning: the approved diagnostic did not meet its "
        f"maximum of {calibration['maximum']} {safe_text(calibration['units'])} "
        "at the approved ceiling; the ceiling count was used."
        if calibration is not None and not calibration["criterion_met"]
        else None
    )
    evaluation = "\n".join(
        (
            f"# Evaluation — Experiment {identifier}",
            "",
            dossier_note,
            "",
            "## Public checks",
            "",
            *_public_check_lines(task, experiment, result),
            "",
            f"## Statistic\n\n{safe_text(result.get('evaluation', {}).get('statistic', ''))}",
            "",
            "## Official comparisons",
            "",
            *_comparison_lines(result),
            "",
            "## Approved aggregate telemetry",
            "",
            *(
                [f"- **{safe_text(name)}:** {value}" for name, value in telemetry.items()]
                or ["- None published."]
            ),
            "",
            f"## Final decision\n\n**{decision}**",
            "",
            f"Champion after: {_code(str(result['champion_after'])[:12])}",
            "",
            *([f"**{calibration_note}**", ""] if calibration_note else []),
            "The approved evaluator calculates uncertainty for each candidate "
            "comparison. arctl validates the protocol and evidence shape, not the "
            "evaluator's mathematics. This adaptive search has no task-wide "
            "false-promotion guarantee.",
            "",
        )
    )
    return {
        "README.md": readme,
        "change.diff": candidate_diff(task.repo, champion, candidate) + "\n",
        "research.md": research,
        "evaluation.md": evaluation,
    }


def ensure_experiment_dossier(
    task_directory: Path,
    task: TaskConfig,
    experiment: Path,
    result: dict[str, Any],
) -> Path:
    """Create a public dossier once; existing derived history is immutable."""
    target = (
        task_directory
        / "reports"
        / "experiments"
        / f"{int(result['experiment_id']):06d}"
    )
    readme = target / "README.md"
    if readme.is_file():
        return readme
    if target.exists():
        raise StateError(f"experiment dossier path is invalid: {target}")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=parent))
    try:
        for name, content in _documents(
            task_directory, task, experiment, result
        ).items():
            atomic_write_text(temporary / name, content)
        try:
            os.rename(temporary, target)
        except FileExistsError:
            if not readme.is_file():
                raise StateError(f"experiment dossier path is invalid: {target}")
        return readme
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
