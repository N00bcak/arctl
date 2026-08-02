from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from arctl.dossier import ensure_experiment_dossier
from arctl.models import TaskConfig

from .helpers import valid_task


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class DossierTests(unittest.TestCase):
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
            self.assertIn("-score = 1", rendered)
            self.assertIn("+score = 2", rendered)

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
