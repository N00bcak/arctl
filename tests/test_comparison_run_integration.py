from __future__ import annotations

import json
import hashlib
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from arctl.comparison import reserve_comparison
from arctl.comparison_run import (
    ComparisonFailure,
    _subject_case_shards,
    run_comparison,
)
from arctl.manifest import EvaluatorManifest

from .test_manifest import valid_manifest

_EVALUATOR = """\
import json
import sys
from pathlib import Path

request = json.loads(Path(sys.argv[1]).read_text())
response = Path(sys.argv[2])
if request["operation"] == "calibrate":
    response.write_text(json.dumps({
        "operation": "calibrate",
        "champion": request["champion"],
        "evaluator": request["evaluator"],
        "manifest": request["manifest"],
        "policy": request["policy"],
        "assessments": [
            {"trial_count": count, "diagnostic_value": 0.5}
            for count in request["ladder"]
        ],
    }))
elif request["operation"] == "prepare":
    cases = [{"value": seed % 100} for seed in request["trial_seeds"]]
    Path(request["public_batch"]).write_text(json.dumps({
        "trial_count": request["trial_count"],
        "cases": cases,
    }))
    Path(request["private_scoring"]).write_text(json.dumps({"prepared": True}))
    response.write_text(json.dumps({
        "operation": "prepare",
        "kind": request["kind"],
        "trial_count": request["trial_count"],
    }))
elif request["operation"] == "score":
    champion = json.loads(Path(request["champion_output"]).read_text())["results"]
    candidate = json.loads(Path(request["candidate_output"]).read_text())["results"]
    differences = [b["score"] - a["score"] for a, b in zip(champion, candidate)]
    effect = sum(differences) / len(differences)
    response.write_text(json.dumps({
        "kind": request["kind"],
        "trial_count": request["trial_count"],
        "hard_rules_pass": True,
        "comparison": {
            "effect_estimate": effect,
            "one_sided_lower_bound": effect,
        },
        "suspect_test": {"required": False, "reason": None},
        "telemetry": {},
    }))
else:
    raise SystemExit(2)
"""

_SUBJECT = """\
import json
import sys
from pathlib import Path

batch = json.loads(Path(sys.argv[1]).read_text())
results = [{"score": case["value"] + BIAS} for case in batch["cases"]]
Path(sys.argv[2]).write_text(json.dumps({
    "trial_count": batch["trial_count"],
    "results": results,
}))
"""


class ComparisonRunIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.evaluator = self.root / "evaluator"
        self.champion = self.root / "champion"
        self.candidate = self.root / "candidate"
        for directory in (self.evaluator, self.champion, self.candidate):
            directory.mkdir()
        (self.evaluator / "evaluator.py").write_text(_EVALUATOR)
        (self.champion / "subject.py").write_text(_SUBJECT.replace("BIAS", "0"))
        (self.candidate / "subject.py").write_text(_SUBJECT.replace("BIAS", "1"))
        raw_manifest = json.dumps(
            valid_manifest(), sort_keys=True, separators=(",", ":")
        ).encode()
        self.manifest_hash = hashlib.sha256(raw_manifest).hexdigest()
        self.manifest = EvaluatorManifest.from_mapping(json.loads(raw_manifest))
        for directory in (self.evaluator, self.champion, self.candidate):
            subprocess.run(["git", "-C", str(directory), "init", "-q"], check=True)
            subprocess.run(
                ["git", "-C", str(directory), "config", "user.name", "arctl tests"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(directory),
                    "config",
                    "user.email",
                    "tests@arctl.invalid",
                ],
                check=True,
            )
            subprocess.run(["git", "-C", str(directory), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(directory), "commit", "-qm", "frozen revision"],
                check=True,
            )
        self.evaluator_commit = self.revision(self.evaluator)
        self.champion_commit = self.revision(self.champion)
        self.candidate_commit = self.revision(self.candidate)
        self.comparison = self.root / "comparison"
        self.reservation = reserve_comparison(
            self.comparison / "reservation.private.json",
            kind="primary",
            experiment_id=1,
            champion=self.champion_commit,
            candidate=self.candidate_commit,
            evaluator=self.evaluator_commit,
            manifest=self.manifest_hash,
            trial_count=4,
            commands={
                "subject": self.manifest.subject_command,
                "prepare": self.manifest.prepare_command,
                "score": self.manifest.score_command,
            },
            master_seed=bytes(range(32)),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def revision(directory: Path) -> str:
        return subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def execute_comparison(self):
        def unconfined(command, cwd, read_paths, write_paths, profile):
            return command

        return run_comparison(
            self.comparison,
            self.reservation,
            self.manifest,
            manifest_hash=self.manifest_hash,
            evaluator_directory=self.evaluator,
            champion_directory=self.champion,
            candidate_directory=self.candidate,
            command_builder=unconfined,
        )

    def test_runs_complete_paired_comparison_and_recovers_saved_evidence(self) -> None:
        evidence = self.execute_comparison()
        self.assertEqual(evidence.effect_estimate, 1)
        self.assertEqual(evidence.one_sided_lower_bound, 1)
        started = sorted(self.comparison.rglob("process/**/started.json"))
        self.assertEqual(len(started), 10)
        timestamps = {path: path.stat().st_mtime_ns for path in started}

        recovered = self.execute_comparison()
        self.assertEqual(recovered, evidence)
        self.assertEqual(
            {path: path.stat().st_mtime_ns for path in started},
            timestamps,
        )

        champion = json.loads(
            (self.comparison / "outputs" / "champion" / "result.json").read_text()
        )
        candidate = json.loads(
            (self.comparison / "outputs" / "candidate" / "result.json").read_text()
        )
        self.assertEqual(len(champion["results"]), 4)
        self.assertEqual(
            [row["score"] - 1 for row in candidate["results"]],
            [row["score"] for row in champion["results"]],
        )

    def test_subject_cases_run_concurrently_and_preserve_order(self) -> None:
        barrier_subject = textwrap.dedent(
            """\
            import json
            import sys
            import time
            from pathlib import Path

            batch = json.loads(Path(sys.argv[1]).read_text())
            output = Path(sys.argv[2])
            ready = output.with_suffix(".ready")
            ready.write_text("ready")
            deadline = time.monotonic() + 2
            while len(tuple(output.parent.parent.glob("*/*.ready"))) < 4:
                if time.monotonic() >= deadline:
                    raise SystemExit("subject shards did not overlap")
                time.sleep(0.01)
            results = [{"score": case["value"] + BIAS} for case in batch["cases"]]
            output.write_text(json.dumps({
                "trial_count": batch["trial_count"],
                "results": results,
            }))
            """
        )
        (self.champion / "subject.py").write_text(barrier_subject.replace("BIAS", "0"))
        (self.candidate / "subject.py").write_text(barrier_subject.replace("BIAS", "1"))
        for directory in (self.champion, self.candidate):
            subprocess.run(["git", "-C", str(directory), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(directory), "commit", "-qm", "parallel subject"],
                check=True,
            )
        object.__setattr__(self.reservation, "champion", self.revision(self.champion))
        object.__setattr__(self.reservation, "candidate", self.revision(self.candidate))

        evidence = self.execute_comparison()

        self.assertEqual(evidence.effect_estimate, 1)
        for subject in ("champion", "candidate"):
            workers = self.comparison / "outputs" / subject / "workers"
            self.assertEqual(len(tuple(workers.glob("*/result.json"))), 4)
            combined = json.loads(
                (self.comparison / "outputs" / subject / "result.json").read_text()
            )
            self.assertEqual(combined["trial_count"], 4)

    def test_subject_cases_support_an_eight_worker_cap(self) -> None:
        shards = _subject_case_shards(tuple(range(64)), subject_workers=8)

        self.assertEqual(len(shards), 8)
        self.assertEqual([len(shard) for shard in shards], [8] * 8)
        self.assertEqual(
            tuple(item for shard in shards for item in shard), tuple(range(64))
        )

    def test_subject_worker_count_is_capped_at_sixteen(self) -> None:
        self.comparison = self.root / "comparison-sixteen-workers"
        self.reservation = reserve_comparison(
            self.comparison / "reservation.private.json",
            kind="primary",
            experiment_id=1,
            champion=self.champion_commit,
            candidate=self.candidate_commit,
            evaluator=self.evaluator_commit,
            manifest=self.manifest_hash,
            trial_count=17,
            commands={
                "subject": self.manifest.subject_command,
                "prepare": self.manifest.prepare_command,
                "score": self.manifest.score_command,
            },
            master_seed=bytes(range(32)),
        )

        evidence = self.execute_comparison()

        self.assertEqual(evidence.effect_estimate, 1)
        for subject in ("champion", "candidate"):
            workers = self.comparison / "outputs" / subject / "workers"
            batches = sorted(workers.glob("*/batch.public.json"))
            self.assertEqual(len(batches), 16)
            self.assertEqual(
                sorted(json.loads(path.read_text())["trial_count"] for path in batches),
                [1] * 15 + [2],
            )
            combined = json.loads(
                (self.comparison / "outputs" / subject / "result.json").read_text()
            )
            self.assertEqual(combined["trial_count"], 17)

    def test_invalid_candidate_output_is_reject_domain_and_never_reruns(self) -> None:
        (self.candidate / "subject.py").write_text(
            "import json,sys; open(sys.argv[2], 'w').write(json.dumps({"
            "'obsolete':1,'trial_count':4,'results':[]}))"
        )
        subprocess.run(["git", "-C", str(self.candidate), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.candidate), "commit", "-qm", "malformed subject"],
            check=True,
        )
        object.__setattr__(
            self.reservation,
            "candidate",
            self.revision(self.candidate),
        )
        with self.assertRaises(ComparisonFailure) as first:
            self.execute_comparison()
        self.assertEqual(first.exception.source, "candidate")
        started = sorted(
            (self.comparison / "process" / "candidate").glob("*/started.json")
        )
        timestamps = {path: path.stat().st_mtime_ns for path in started}

        with self.assertRaises(ComparisonFailure) as second:
            self.execute_comparison()
        self.assertEqual(second.exception.source, "candidate")
        self.assertEqual(
            {path: path.stat().st_mtime_ns for path in started},
            timestamps,
        )

    def test_tampered_reservation_commands_fail_before_any_process(self) -> None:
        object.__setattr__(
            self.reservation,
            "commands",
            {**self.reservation.commands, "score": ("python3", "other.py")},
        )
        with self.assertRaisesRegex(
            ComparisonFailure, "differ from the manifest"
        ) as error:
            self.execute_comparison()
        self.assertEqual(error.exception.source, "evidence")
        self.assertFalse((self.comparison / "process").exists())

    def test_sandbox_launch_failure_is_not_attributed_to_candidate(self) -> None:
        def fail_candidate_sandbox(command, cwd, _reads, _writes, profile):
            if profile == "arctl-subject" and cwd == self.candidate:
                return ("false",)
            return command

        with self.assertRaisesRegex(
            ComparisonFailure, "sandbox did not start"
        ) as error:
            run_comparison(
                self.comparison,
                self.reservation,
                self.manifest,
                manifest_hash=self.manifest_hash,
                evaluator_directory=self.evaluator,
                champion_directory=self.champion,
                candidate_directory=self.candidate,
                command_builder=fail_candidate_sandbox,
            )
        self.assertEqual(error.exception.source, "sandbox")

    def test_wrong_frozen_revision_fails_before_any_process(self) -> None:
        (self.candidate / "subject.py").write_text(_SUBJECT.replace("BIAS", "2"))
        subprocess.run(["git", "-C", str(self.candidate), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.candidate), "commit", "-qm", "post-freeze change"],
            check=True,
        )
        with self.assertRaisesRegex(
            ComparisonFailure, "differs from the reservation"
        ) as error:
            self.execute_comparison()
        self.assertEqual(error.exception.source, "candidate")
        self.assertFalse((self.comparison / "process").exists())
