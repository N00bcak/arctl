from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arctl.candidate_review import review_candidate
from arctl.errors import ResearchMiss, TransientDownstreamError
from arctl.manifest import EvaluatorManifest
from arctl.models import TaskConfig
from arctl.registry import LocatedTask

from .helpers import valid_task
from .test_manifest import valid_manifest


class CandidateReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        raw = valid_task()
        raw["repo"] = str(self.worktree)
        raw["candidate_review"] = {
            "contract": "Use only supplied observations and no privileged inputs.",
            "checks": [
                [
                    "python3",
                    "-c",
                    "from pathlib import Path; raise SystemExit(Path('bad').exists())",
                ]
            ],
            "repair_attempts": 1,
        }
        self.task = LocatedTask(self.root / "task", TaskConfig.from_mapping(raw))
        self.manifest = EvaluatorManifest.from_mapping(valid_manifest())
        self.request = {"claim": "improve", "mechanism": "change policy"}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def check(command, _worktree, _scratch):
        return command

    @staticmethod
    def passing_review(worktree: Path, scratch: Path, _schema: Path, prompt: str):
        script = """\
import json, sys
from pathlib import Path
scratch = Path(sys.argv[1])
(scratch / 'review.public.json').write_text(json.dumps({
    'schema_version': 1,
    'summary': 'The candidate obeys the supplied-observation contract.',
    'findings': [],
}))
"""
        return ("python3", "-c", script, str(scratch))

    def test_passing_review_is_saved_without_repair(self) -> None:
        events: list[dict] = []
        review = review_candidate(
            self.task,
            self.manifest,
            worktree=self.worktree,
            attempt_directory=self.root / "attempt",
            champion="a" * 40,
            request=self.request,
            stop_path=self.root / "stop",
            review_command_builder=self.passing_review,
            check_command_builder=self.check,
            progress=events.append,
        )
        assert review is not None
        self.assertEqual(review["verdict"], "pass")
        self.assertEqual([event["event"] for event in events], ["candidate_review"])

    def test_transient_policy_check_uses_a_fresh_process_attempt(self) -> None:
        calls = 0

        def check(_command, _worktree, _scratch):
            nonlocal calls
            calls += 1
            if calls == 1:
                return (
                    "python3",
                    "-c",
                    "import sys; print('HTTP Error 503: Service Unavailable', file=sys.stderr); raise SystemExit(1)",
                )
            return ("true",)

        arguments = {
            "task": self.task,
            "manifest": self.manifest,
            "worktree": self.worktree,
            "attempt_directory": self.root / "attempt",
            "champion": "a" * 40,
            "request": self.request,
            "stop_path": self.root / "stop",
            "review_command_builder": self.passing_review,
            "check_command_builder": check,
        }
        with self.assertRaises(TransientDownstreamError):
            review_candidate(**arguments)
        result = review_candidate(**arguments)

        assert result is not None
        checks = (
            self.root
            / "attempt"
            / "candidate-review"
            / "round-01"
            / "checks"
        )
        self.assertTrue((checks / "0001" / "process" / "stderr.bin").is_file())
        self.assertTrue(
            (checks / "0001-retry-0001" / "process" / "result.json").is_file()
        )

    def test_reviewer_schema_has_no_redundant_verdict(self) -> None:
        prompts: list[str] = []

        def review(_worktree: Path, scratch: Path, schema: Path, prompt: str):
            prompts.append(prompt)
            self.assertNotIn("verdict", json.loads(schema.read_text())["properties"])
            (scratch / "review.public.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "summary": "The candidate obeys the contract.",
                        "findings": [],
                    }
                )
            )
            return ("true",)

        result = review_candidate(
            self.task,
            self.manifest,
            worktree=self.worktree,
            attempt_directory=self.root / "attempt",
            champion="a" * 40,
            request=self.request,
            stop_path=self.root / "stop",
            implementation_report={
                "schema_version": 2,
                "status": "implemented",
                "summary": "Implemented the mechanism.",
                "deviations": [],
                "requirements": [
                    {
                        "requirement": "Preserve deterministic behavior.",
                        "status": "verified",
                        "evidence": "policy.py:10",
                    }
                ],
            },
            review_command_builder=review,
            check_command_builder=self.check,
        )

        assert result is not None
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["findings"], [])
        self.assertEqual(len(prompts), 1)
        self.assertIn("findings array is exclusively", prompts[0])
        self.assertIn("Preserve deterministic behavior.", prompts[0])
        self.assertIn("report every independently supported violation", prompts[0])

    def test_default_reviewer_reuses_ambient_authenticated_codex_home(self) -> None:
        seen_environment: list[dict[str, str]] = []

        def completed(_directory, command, **arguments):
            if "--output-last-message" in command:
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "summary": "The candidate obeys the contract.",
                            "findings": [],
                        }
                    )
                )
                seen_environment.append(arguments["env"])
            return {"return_code": 0}

        with (
            mock.patch.dict(
                os.environ,
                {"CODEX_HOME": str(self.root / "authenticated-codex")},
            ),
            mock.patch(
                "arctl.candidate_review.run_or_load_once",
                side_effect=completed,
            ),
        ):
            for _ in range(2):
                review_candidate(
                    self.task,
                    self.manifest,
                    worktree=self.worktree,
                    attempt_directory=self.root / "attempt",
                    champion="a" * 40,
                    request=self.request,
                    stop_path=self.root / "stop",
                )

        self.assertEqual(
            seen_environment[0]["CODEX_HOME"],
            str((self.root / "authenticated-codex").resolve()),
        )
        self.assertNotIn("candidate-review", seen_environment[0]["CODEX_HOME"])
        self.assertEqual(len(seen_environment), 2)

    def test_tripwire_failure_gets_one_repair_then_semantic_review(self) -> None:
        (self.worktree / "bad").write_text("violation")
        review_prompts: list[str] = []

        def repair(_worktree: Path, scratch: Path, _schema: Path, _prompt: str):
            script = """\
import json, sys
from pathlib import Path
worktree, scratch = map(Path, sys.argv[1:])
(worktree / 'bad').unlink()
(scratch / 'repair.public.json').write_text(json.dumps({
    'schema_version': 2,
    'status': 'repaired',
    'summary': 'Removed the prohibited access.',
    'requirements': [{
        'requirement': 'Use only supplied observations.',
        'status': 'verified',
        'evidence': 'bad was removed and the policy check passes.',
    }],
}))
"""
            return ("python3", "-c", script, str(self.worktree), str(scratch))

        def review(worktree: Path, scratch: Path, schema: Path, prompt: str):
            review_prompts.append(prompt)
            return self.passing_review(worktree, scratch, schema, prompt)

        events: list[dict] = []
        review = review_candidate(
            self.task,
            self.manifest,
            worktree=self.worktree,
            attempt_directory=self.root / "attempt",
            champion="a" * 40,
            request=self.request,
            stop_path=self.root / "stop",
            review_command_builder=review,
            repair_command_builder=repair,
            check_command_builder=self.check,
            progress=events.append,
        )
        assert review is not None
        self.assertEqual(review["verdict"], "pass")
        self.assertEqual(
            [event["event"] for event in events],
            ["candidate_review", "candidate_repair", "candidate_review"],
        )
        self.assertIn("Use only supplied observations.", review_prompts[0])
        self.assertIn("bad was removed", review_prompts[0])

    def test_infeasible_repair_ends_the_attempt(self) -> None:
        (self.worktree / "bad").write_text("violation")

        def infeasible(_worktree: Path, scratch: Path, _schema: Path, _prompt: str):
            script = """\
import json, sys
from pathlib import Path
scratch = Path(sys.argv[1])
(scratch / 'repair.public.json').write_text(json.dumps({
    'schema_version': 2,
    'status': 'infeasible',
    'summary': 'The mechanism cannot obey the interface.',
    'requirements': [{
        'requirement': 'Use only supplied observations.',
        'status': 'unverified',
        'evidence': 'The required input is not supplied.',
    }],
}))
"""
            return ("python3", "-c", script, str(scratch))

        with self.assertRaisesRegex(
            ResearchMiss, "mechanism cannot obey the interface"
        ):
            review_candidate(
                self.task,
                self.manifest,
                worktree=self.worktree,
                attempt_directory=self.root / "attempt",
                champion="a" * 40,
                request=self.request,
                stop_path=self.root / "stop",
                review_command_builder=self.passing_review,
                repair_command_builder=infeasible,
                check_command_builder=self.check,
            )

    def test_second_tripwire_failure_is_a_research_miss(self) -> None:
        (self.worktree / "bad").write_text("violation")

        def ineffective_repair(_worktree: Path, scratch: Path, _schema: Path, _prompt: str):
            script = """\
import json, sys
from pathlib import Path
scratch = Path(sys.argv[1])
(scratch / 'repair.public.json').write_text(json.dumps({
    'schema_version': 1,
    'summary': 'No effective change.',
}))
"""
            return ("python3", "-c", script, str(scratch))

        with self.assertRaisesRegex(ResearchMiss, "candidate_check_1") as raised:
            review_candidate(
                self.task,
                self.manifest,
                worktree=self.worktree,
                attempt_directory=self.root / "attempt",
                champion="a" * 40,
                request=self.request,
                stop_path=self.root / "stop",
                review_command_builder=self.passing_review,
                repair_command_builder=ineffective_repair,
                check_command_builder=self.check,
            )
        self.assertEqual(
            raised.exception.details["candidate_review"]["findings"][0]["rule"],
            "candidate_check_1",
        )


if __name__ == "__main__":
    unittest.main()
