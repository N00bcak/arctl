from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from arctl.dossier import (
    _v4_reflection_document,
    ensure_experiment_dossier,
    rebuild_task_index,
)
from arctl.models import TaskConfig

from .helpers import valid_task
from .test_manifest import valid_manifest


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class DossierTests(unittest.TestCase):
    def test_v4_reflection_omits_empty_nonmaterial_sections(self) -> None:
        rendered = _v4_reflection_document(
            1,
            {
                "schema_version": 4,
                "summary": "No material causal signal emerged.",
                "strategy_behavior": {
                    "id": "safe-action",
                    "realization": "unclear",
                    "evidence": [],
                },
                "material_signals": [],
                "mechanism": {
                    "status": "not_demonstrated",
                    "evidence": [],
                    "missing_evidence": [],
                },
                "implementation": {
                    "status": "no_specific_concern",
                    "concerns": [],
                },
                "policy_observations": [],
                "next_action": {"kind": "retain", "rationale": "No change."},
            },
            "Derived view.",
        )

        self.assertIn("## Summary", rendered)
        self.assertNotIn("## Material signals", rendered)
        self.assertNotIn("## Mechanism", rendered)
        self.assertNotIn("## Implementation", rendered)
        self.assertNotIn("Specific test:", rendered)

    def test_task_index_rebuilds_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            result = {
                "experiment_id": 1,
                "decision": "ACCEPT",
                "champion_before": "a" * 40,
                "champion_after": "b" * 40,
                "evaluation": {
                    "comparisons": [
                        {"effect_estimate": 1.0, "one_sided_lower_bound": 0.2}
                    ]
                },
            }
            index = rebuild_task_index(task, [result])
            first = index.read_bytes()
            rebuild_task_index(task, [result])

            self.assertEqual(index.read_bytes(), first)
            self.assertIn("Current champion", index.read_text())
            self.assertIn("experiments/000001/README.md", index.read_text())

    def test_creates_immutable_public_only_human_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "subject"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.name", "arctl tests")
            git(repo, "config", "user.email", "tests@arctl.invalid")
            (repo / "agent.py").write_text("score = 1\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "champion")
            champion = git(repo, "rev-parse", "HEAD")
            (repo / "agent.py").write_text("score = 2\n")
            git(repo, "commit", "-qam", "candidate")
            candidate = git(repo, "rev-parse", "HEAD")

            raw_task = valid_task()
            raw_task["repo"] = str(repo)
            raw_task["editable_paths"] = ["**"]
            raw_task["public_checks"] = [["python3", "-m", "unittest"]]
            task = TaskConfig.from_mapping(raw_task)
            task_directory = root / "task"
            experiment = task_directory / "experiments" / "000001"
            manifest = valid_manifest(telemetry=True)
            manifest["public"]["telemetry"]["public_metric"] = (
                manifest["public"]["telemetry"].pop("errors")
            )
            (task_directory / "evaluator.manifest.json").parent.mkdir(
                parents=True, exist_ok=True
            )
            (task_directory / "evaluator.manifest.json").write_text(
                json.dumps(manifest)
            )
            (experiment / "process" / "public-check-0001").mkdir(parents=True)
            (experiment / "process" / "public-check-0001" / "result.json").write_text(
                json.dumps({"schema_version": 1, "return_code": 0})
            )
            (experiment / "request.public.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "strategy_behavior_id": "improve-score",
                        "claim": "<img src=https://invalid.example> improve score",
                        "mechanism": "Increase score.",
                        "viability": "The score path is adjustable.",
                        "evidence_review": {
                            "summary": "No prior evidence.",
                            "citations": [],
                        },
                        "expected_effect": "Higher score.",
                        "expected_telemetry": {"public_metric": "increase"},
                        "falsifiers": ["Effect is not positive."],
                        "lineage": {"kind": "new", "prior_entry_id": None},
                    }
                )
            )
            (experiment / "implementation.public.json").write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "status": "implemented",
                        "summary": "Implemented the score change.",
                        "deviations": [],
                        "requirements": [
                            {
                                "id": "score-change",
                                "requirement": "Increase the score.",
                                "status": "verified",
                                "evidence": "The targeted probe observes score 2.",
                                "verification_ids": ["score-probe"],
                            }
                        ],
                        "verifications": [
                            {
                                "id": "score-probe",
                                "purpose": "Exercise the changed score path.",
                                "command": "python3 -c 'import agent; assert agent.score == 2'",
                                "outcome": "passed",
                                "evidence": "The command exited successfully.",
                            }
                        ],
                    }
                )
            )
            (experiment / "planning.public.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "directions": [
                            {
                                "strategy_behavior_id": "improve-score",
                                "champion_assessment": "The score path is active.",
                                "remaining_gap": "Its offset remains improvable.",
                                "disposition": "candidate",
                                "request": json.loads(
                                    (experiment / "request.public.json").read_text()
                                ),
                                "evidence": ["The offset is editable."],
                                "feasibility": "A one-line diff and one probe; negligible runtime and audit burden.",
                                "expected_value": "A material score gain for little implementation cost.",
                            }
                        ],
                        "selection_rationale": "Best expected improvement per implementation and audit cost.",
                        "selection": "improve-score",
                    }
                )
            )
            (experiment / "implementation-origin.public.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "transcript": "searches/000001/attempts/01/implementation/attempts/0001/process/stdout.bin",
                    }
                )
            )
            (experiment / "reflection.public.json").write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "status": "COMPLETE",
                        "warning": None,
                        "basis": {"strategy_behavior_id": "improve-score"},
                        "assessment": {
                            "summary": "The public metric supports the mechanism.",
                            "strategy_behavior": {
                                "id": "improve-score",
                                "realization": "expressed",
                                "evidence": [],
                            },
                            "metric_assessments": {
                                "public_metric": {
                                    "finding": "supports",
                                    "rationale": "The candidate value increased.",
                                }
                            },
                            "mechanism": {
                                "status": "supported",
                                "evidence": [],
                                "missing_evidence": [],
                            },
                            "implementation": {
                                "status": "no_specific_concern",
                                "evidence": [],
                                "concerns": [],
                            },
                            "policy_observations": [],
                            "next_action": {
                                "kind": "retain",
                                "rationale": "The result supports retention.",
                                "test": "Continue with another mechanism.",
                            },
                            "history_citations": [],
                            "schema_version": 3,
                        },
                    }
                )
            )
            secret = "PRIVATE-SEED-MUST-NOT-LEAK"
            (experiment / "reservation.private.json").write_text(secret)
            result = {
                "experiment_id": 1,
                "hypothesis": "<img src=https://invalid.example> improve score",
                "champion_before": champion,
                "candidate": candidate,
                "champion_after": candidate,
                "decision": "ACCEPT",
                "evaluation": {
                    "statistic": "paired score difference",
                    "comparisons": [
                        {
                            "kind": "primary",
                            "trials": 8,
                            "effect_estimate": 1.0,
                            "one_sided_lower_bound": 0.5,
                            "suspect_test_required": False,
                            "suspect_test_reason": None,
                        }
                    ],
                },
                "constraints": {"tests": "PASS"},
                "telemetry": {
                    "public_metric": {
                        "champion": 1.0,
                        "candidate": 2.0,
                        "delta": 1.0,
                    }
                },
            }

            readme = ensure_experiment_dossier(
                task_directory,
                task,
                experiment,
                result,
            )
            files = sorted(readme.parent.iterdir())
            self.assertEqual(
                [path.name for path in files],
                [
                    "README.md",
                    "change.diff",
                    "evaluation.md",
                    "implementation.md",
                    "reflection.md",
                    "research.md",
                ],
            )
            rendered = "\n".join(path.read_text() for path in files)
            self.assertNotIn(secret, rendered)
            self.assertNotIn("reservation.private", rendered)
            self.assertNotIn("<img", rendered)
            self.assertIn("&lt;img", rendered)
            self.assertIn("public_metric", rendered)
            self.assertIn("The candidate value increased", rendered)
            self.assertIn("-score = 1", rendered)
            self.assertIn("+score = 2", rendered)
            self.assertIn("Agent-reported verification disclosure", rendered)
            self.assertIn("python3 -c", rendered)
            self.assertIn("stdout.bin", rendered)
            self.assertIn("Admission reasoning", rendered)
            self.assertIn("negligible runtime and audit burden", rendered)
            self.assertIn("Best expected improvement per implementation", rendered)

            readme.write_text("do not rewrite\n")
            self.assertEqual(
                ensure_experiment_dossier(
                    task_directory,
                    task,
                    experiment,
                    result,
                ).read_text(),
                "do not rewrite\n",
            )
