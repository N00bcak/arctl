"""Public-only human-readable views of completed experiments."""

from __future__ import annotations

import html
import json
import os
import re
import shlex
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import StateError
from .git import candidate_changed_paths, candidate_diff
from .models import TaskConfig
from .reflection import validate_reflection
from .storage import atomic_write_text
from .taskio import load_manifest
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


def _implementation_document(
    identifier: int,
    disclosures: list[tuple[str, Mapping[str, Any]]],
    origin: Mapping[str, Any] | None,
    dossier_note: str,
) -> str:
    lines = [
        f"# Implementation disclosure — Experiment {identifier}",
        "",
        dossier_note,
        "",
        "> Agent-reported verification disclosure. arctl retains these commands and ",
        "> observations for audit and replay but does not rerun them. They are not ",
        "> independent causal attribution.",
        "",
    ]
    if origin is not None:
        transcript = origin.get("transcript") or origin.get("process_directory")
        if transcript:
            lines.extend(
                [
                    "Authoritative process evidence: " + _code(transcript),
                    "",
                ]
            )
    for label, report in disclosures:
        lines.extend(
            [
                f"## {safe_text(label)}",
                "",
                f"**Status:** **{safe_text(report.get('status', 'unknown'))}**",
                "",
                safe_text(report.get("summary", "")),
                "",
                "### Frozen requirements",
                "",
            ]
        )
        requirements = report.get("requirements", [])
        lines.extend(
            [
                f"- **{safe_text(item.get('id', 'historical'))} — "
                f"{safe_text(item.get('status', 'unknown'))}:** "
                f"{safe_text(item.get('requirement', ''))} "
                f"Evidence: {safe_text(item.get('evidence', ''))}"
                for item in requirements
                if isinstance(item, Mapping)
            ]
            or ["- Structured requirement audit was unavailable for this historical report."]
        )
        lines.extend(["", "### Targeted verification", ""])
        verifications = report.get("verifications", [])
        if verifications:
            for item in verifications:
                if not isinstance(item, Mapping):
                    continue
                lines.extend(
                    [
                        f"#### {safe_text(item.get('id', 'unknown'))}",
                        "",
                        f"Purpose: {safe_text(item.get('purpose', ''))}",
                        "",
                        "Exact command: " + _code(item.get("command", "")),
                        "",
                        f"Observed outcome: **{safe_text(item.get('outcome', 'unknown'))}**",
                        "",
                        f"Evidence: {safe_text(item.get('evidence', ''))}",
                        "",
                    ]
                )
        else:
            lines.extend(
                [
                    "Structured replay information was unavailable for this historical report.",
                    "",
                ]
            )
    return "\n".join(lines)


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


def _telemetry_lines(telemetry: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for name, raw in telemetry.items():
        if isinstance(raw, dict) and set(raw) == {"champion", "candidate", "delta"}:
            lines.append(
                f"- **{safe_text(name)}:** champion {raw['champion']}; "
                f"candidate {raw['candidate']}; delta {raw['delta']}"
            )
        elif isinstance(raw, dict) and set(raw) == {"value"}:
            lines.append(f"- **{safe_text(name)}:** {raw['value']}")
        else:
            lines.append(f"- **{safe_text(name)}:** invalid derived value")
    return lines


def _v4_reflection_document(
    identifier: int, assessment: Mapping[str, Any], dossier_note: str
) -> str:
    lines = [
        f"# Post-trial reflection — Experiment {identifier}",
        "",
        dossier_note,
        "",
        "## Summary",
        "",
        safe_text(assessment.get("summary", "")),
        "",
    ]
    behavior = assessment.get("strategy_behavior", {})
    if isinstance(behavior, Mapping):
        lines.extend(
            [
                "## Strategic behavior",
                "",
                f"`{safe_text(behavior.get('id', ''))}` — "
                f"**{safe_text(behavior.get('realization', 'unknown'))}**",
                "",
                *[f"- {safe_text(item)}" for item in behavior.get("evidence", [])],
                "",
            ]
        )
    signals = assessment.get("material_signals", [])
    if signals:
        lines.extend(["## Material signals", ""])
        lines.extend(
            f"- **{safe_text(item.get('metric', ''))} — "
            f"{safe_text(item.get('finding', ''))}:** "
            f"{safe_text(item.get('interpretation', ''))}"
            for item in signals
        )
        lines.append("")
    mechanism = assessment.get("mechanism", {})
    if isinstance(mechanism, Mapping) and (
        mechanism.get("evidence")
        or mechanism.get("missing_evidence")
        or mechanism.get("status") != "not_demonstrated"
    ):
        lines.extend(
            [
                "## Mechanism",
                "",
                f"**{safe_text(mechanism.get('status', 'unknown'))}**",
                "",
                *[f"- {safe_text(item)}" for item in mechanism.get("evidence", [])],
                *[
                    f"- Missing: {safe_text(item)}"
                    for item in mechanism.get("missing_evidence", [])
                ],
                "",
            ]
        )
    implementation = assessment.get("implementation", {})
    if isinstance(implementation, Mapping) and (
        implementation.get("concerns")
        or implementation.get("status") != "no_specific_concern"
    ):
        lines.extend(
            [
                "## Implementation",
                "",
                f"**{safe_text(implementation.get('status', 'unknown'))}**",
                "",
                *[
                    f"- Concern: {safe_text(item)}"
                    for item in implementation.get("concerns", [])
                ],
                "",
            ]
        )
    observations = assessment.get("policy_observations", [])
    if observations:
        lines.extend(["## Policy observations", ""])
        lines.extend(
            f"- **{safe_text(item.get('finding', ''))}:** "
            f"{safe_text(item.get('evidence', ''))} "
            f"Implication: {safe_text(item.get('implication', ''))}"
            for item in observations
        )
        lines.append("")
    action = assessment.get("next_action", {})
    lines.extend(
        [
            "## Advisory next action",
            "",
            f"**{safe_text(action.get('kind', 'unknown'))}:** "
            f"{safe_text(action.get('rationale', ''))}",
        ]
    )
    if action.get("test"):
        lines.extend(["", f"Specific test: {safe_text(action['test'])}"])
    lines.append("")
    return "\n".join(lines)


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
            "- Public constraints: **"
            f"{safe_text(result.get('constraints', {}).get('tests', 'unknown'))}**"
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
    review_files = sorted(
        (experiment / "candidate-review").glob("round-*/decision.public.json")
    )
    review = (
        _read_object(review_files[-1], "public candidate review")
        if review_files
        else None
    )
    disclosures: list[tuple[str, Mapping[str, Any]]] = []
    implementation_path = experiment / "implementation.public.json"
    if implementation_path.is_file():
        disclosures.append(
            ("Initial implementation", _read_object(implementation_path, "implementation report"))
        )
    for repair_path in sorted(
        (experiment / "candidate-review").glob("round-*/repair/repair.public.json")
    ):
        disclosures.append(
            (
                f"Candidate repair {repair_path.parents[1].name}",
                _read_object(repair_path, "candidate repair report"),
            )
        )
    origin_path = experiment / "implementation-origin.public.json"
    implementation_origin = (
        _read_object(origin_path, "implementation process origin")
        if origin_path.is_file()
        else None
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
            *(["- [Implementation disclosure](implementation.md)"] if disclosures else []),
            *(
                ["- [Pre-trial policy review](candidate-review.md)"]
                if review
                else []
            ),
            "- [Exact candidate change](change.diff)",
            "- [Checks and official evaluation](evaluation.md)",
            "- [Post-trial reflection](reflection.md)",
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
            f"**Strategic behavior:** `{safe_text(request.get('strategy_behavior_id', ''))}`",
            "",
            f"## Claim\n\n{safe_text(request.get('claim', ''))}",
            "",
            f"## Mechanism\n\n{safe_text(request.get('mechanism', ''))}",
            "",
            f"## Viability\n\n{safe_text(request.get('viability', ''))}",
            "",
            "## Prior evidence review",
            "",
            safe_text(request.get("evidence_review", {}).get("summary", "")),
            "",
            *(
                [
                    f"- `{safe_text(item.get('entry_id', ''))}` "
                    f"**{safe_text(item.get('bearing', ''))}:** "
                    f"{safe_text(item.get('finding', ''))}"
                    for item in request.get("evidence_review", {}).get("citations", [])
                ]
                or ["- No relevant prior entry cited."]
            ),
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
    reflection = None
    if (experiment / "reflection.public.json").is_file():
        manifest, _ = load_manifest(task_directory / "evaluator.manifest.json")
        reflection = validate_reflection(
            _read_object(
                experiment / "reflection.public.json", "public reflection"
            ),
            metric_names=tuple(manifest.public_telemetry),
        )
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
                _telemetry_lines(telemetry)
                or ["- None published."]
            ),
            "",
            *(
                [
                    "## Execution failure",
                    "",
                    safe_text(result["failure_detail"]),
                    "",
                ]
                if result.get("failure_detail")
                else []
            ),
            *(
                [
                    "## Operational assessment",
                    "",
                    safe_text(result["operational_assessment"]["summary"]),
                    "",
                    safe_text(
                        result["operational_assessment"]["scientific_interpretation"]
                    ),
                    "",
                    "Next action: "
                    + safe_text(result["operational_assessment"]["next_action"]),
                    "",
                ]
                if result.get("operational_assessment")
                else []
            ),
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
    if reflection is None:
        reflection_document = "# Post-trial reflection\n\nNot available.\n"
    elif reflection.get("status") == "SKIPPED_NO_TELEMETRY":
        reflection_document = "\n".join(
            (
                f"# Post-trial reflection — Experiment {identifier}",
                "",
                f"**Warning:** {safe_text(reflection.get('warning', ''))}",
                "",
            )
        )
    else:
        assessment = reflection.get("assessment", {})
        if assessment.get("schema_version") == 4:
            reflection_document = _v4_reflection_document(
                identifier, assessment, dossier_note
            )
        else:
            metric_assessments = assessment.get("metric_assessments", [])
            if isinstance(metric_assessments, Mapping):
                metric_items = (
                    {"metric": name, **item}
                    for name, item in metric_assessments.items()
                    if isinstance(item, Mapping)
                )
            else:
                metric_items = metric_assessments
            metric_lines = [
                f"- **{safe_text(item.get('metric', ''))} — "
                f"{safe_text(item.get('finding', ''))}:** "
                f"{safe_text(item.get('rationale', ''))}"
                for item in metric_items
            ]
            mechanism = assessment.get("mechanism", {})
            implementation = assessment.get("implementation", {})
            behavior = assessment.get("strategy_behavior", {})
            policy_observations = assessment.get("policy_observations", [])
            action = assessment.get("next_action", {})
            reflection_document = "\n".join(
                (
                    f"# Post-trial reflection — Experiment {identifier}",
                    "",
                    dossier_note,
                    "",
                    f"## Summary\n\n{safe_text(assessment.get('summary', ''))}",
                    "",
                    "## Strategic behavior",
                    "",
                    f"`{safe_text(behavior.get('id', ''))}` — "
                    f"**{safe_text(behavior.get('realization', 'unknown'))}**",
                    "",
                    *[f"- {safe_text(item)}" for item in behavior.get("evidence", [])],
                    "",
                    "## Telemetry assessment",
                    "",
                    *(metric_lines or ["- No metrics assessed."]),
                    "",
                    "## Mechanism",
                    "",
                    f"**{safe_text(mechanism.get('status', 'unknown'))}**",
                    "",
                    *[f"- {safe_text(item)}" for item in mechanism.get("evidence", [])],
                    *[
                        f"- Missing: {safe_text(item)}"
                        for item in mechanism.get("missing_evidence", [])
                    ],
                    "",
                    "## Implementation",
                    "",
                    f"**{safe_text(implementation.get('status', 'unknown'))}**",
                    "",
                    *[f"- {safe_text(item)}" for item in implementation.get("evidence", [])],
                    *[f"- Concern: {safe_text(item)}" for item in implementation.get("concerns", [])],
                    "",
                    "## Policy observations",
                    "",
                    *(
                        [
                            f"- **{safe_text(item.get('finding', ''))}:** "
                            f"{safe_text(item.get('evidence', ''))} "
                            f"Implication: {safe_text(item.get('implication', ''))}"
                            for item in policy_observations
                        ]
                        or ["- None recorded."]
                    ),
                    "",
                    "## Advisory next action",
                    "",
                    f"**{safe_text(action.get('kind', 'unknown'))}:** "
                    f"{safe_text(action.get('rationale', ''))}",
                    "",
                    f"Suggested test: {safe_text(action.get('test', ''))}",
                    "",
                )
            )
    documents = {
        "README.md": readme,
        "change.diff": candidate_diff(task.repo, champion, candidate) + "\n",
        "research.md": research,
        "evaluation.md": evaluation,
        "reflection.md": reflection_document,
    }
    if disclosures:
        documents["implementation.md"] = _implementation_document(
            identifier,
            disclosures,
            implementation_origin,
            dossier_note,
        )
    if review is not None:
        documents["candidate-review.md"] = "\n".join(
            (
                f"# Candidate review — Experiment {identifier}",
                "",
                dossier_note,
                "",
                f"**Verdict:** **{safe_text(review.get('verdict', 'unknown')).upper()}**",
                "",
                safe_text(review.get("summary", "")),
                "",
                "## Findings",
                "",
                *(
                    [
                        f"- **{safe_text(item.get('rule', ''))}:** "
                        f"{safe_text(item.get('evidence', ''))} "
                        f"Remediation: {safe_text(item.get('remediation', ''))}"
                        for item in review.get("findings", [])
                    ]
                    or ["- None."]
                ),
                "",
            )
        )
    return documents


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


def rebuild_task_index(task_directory: Path, results: list[dict[str, Any]]) -> Path:
    """Rebuild the deterministic, derived navigation index."""
    ordered = sorted(results, key=lambda item: int(item["experiment_id"]))
    current = ordered[-1].get("champion_after") if ordered else None
    lines = ["# Research report index", ""]
    if current is None:
        lines.extend(["Current champion: not established.", ""])
    else:
        lines.extend([f"Current champion: {_code(str(current)[:12])}", ""])
    lines.extend(
        [
            "## Experiment outcomes",
            "",
            "| Experiment | Decision | Operational | Scientific | Effect | Lower bound | Dossier |",
            "| ---: | --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    progression: list[str] = []
    for result in ordered:
        identifier = int(result["experiment_id"])
        comparisons = result.get("evaluation", {}).get("comparisons", [])
        final = comparisons[-1] if comparisons else {}
        lines.append(
            "| "
            + " | ".join(
                (
                    f"{identifier:06d}",
                    safe_text(result.get("decision", "unknown")),
                    safe_text(result.get("operational_status", "—")),
                    safe_text(result.get("scientific_status", "—")),
                    str(final.get("effect_estimate", "—")),
                    str(final.get("one_sided_lower_bound", "—")),
                    f"[details](experiments/{identifier:06d}/README.md)",
                )
            )
            + " |"
        )
        before = result.get("champion_before")
        after = result.get("champion_after")
        if before is not None and after is not None and before != after:
            progression.append(
                f"- Experiment {identifier:06d}: {_code(str(before)[:12])} → "
                f"{_code(str(after)[:12])}"
            )
    lines.extend(["", "## Champion progression", ""])
    lines.extend(progression or ["- No champion changes recorded."])
    lines.append("")
    target = task_directory / "reports" / "README.md"
    atomic_write_text(target, "\n".join(lines))
    return target
