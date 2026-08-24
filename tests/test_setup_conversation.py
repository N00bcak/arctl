from __future__ import annotations

import json
import hashlib
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arctl.cli import _data_root, _init, _print_question_batch, _setup
from arctl.errors import StateError, ValidationError
from arctl.setup import (
    QUESTION_IDS,
    SETUP_CONTROLLER_CONTRACT,
    _validate_dependency_plan,
    _validate_review_evidence,
    build_setup_direct,
    initialize_setup,
    load_setup,
)
from arctl.storage import atomic_write_json
from arctl.setup_conversation import (
    answer_batch,
    authorize_design,
    finalize_design,
    load_decisions,
    render_setup_note,
    save_batch,
    validate_batch,
)


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class SetupConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "subject"
        self.source.mkdir()
        git(self.source, "init", "-q")
        git(self.source, "config", "user.name", "tests")
        git(self.source, "config", "user.email", "tests@invalid")
        (self.source / "README.md").write_text("# Demo\nCPU-only environment.\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "baseline")
        self.workspace = self.root / "subject-research"
        self.data = self.workspace / ".arctl-data"
        initialize_setup(
            data_root=self.data,
            workspace=self.workspace,
            source_repo=self.source,
            task_id="demo",
        )
        self.directory, self.record = load_setup(self.data, "demo")
        self.subject = Path(self.record["subject"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def option(self, identifier: str, value: str) -> dict:
        return {
            "id": identifier,
            "label": value,
            "value": value,
            "consequence": f"Use {value}.",
            "citations": [
                {
                    "kind": "repository",
                    "path": "README.md",
                    "location": "line 2",
                    "finding": "The environment is documented as CPU-only.",
                }
            ],
        }

    def question_batch(self, revision: int = 1) -> dict:
        questions = []
        for identifier in ("objective", "outcome", "policy_boundary"):
            options = [self.option(f"{identifier}_a", "First"), self.option(f"{identifier}_b", "Second")]
            questions.append(
                {
                    "id": identifier,
                    "prompt": f"Choose {identifier}.",
                    "why": "This changes the research contract.",
                    "options": options,
                    "recommended_option_id": options[0]["id"],
                    "allow_custom": True,
                }
            )
        return {
            "schema_version": 1,
            "revision": revision,
            "summary": "Three consequential choices remain.",
            "questions": questions,
            "design": None,
        }

    def design_batch(self, revision: int = 2) -> dict:
        provenance = {
            "source": "derived",
            "decision_refs": [],
            "citations": [],
        }
        return {
            "schema_version": 1,
            "revision": revision,
            "summary": "The setup design is complete.",
            "questions": [],
            "design": {
                "summary": "Optimize the selected outcome within the selected boundary.",
                "objective": {
                    "value": "Improve the demo policy.", "source": "human",
                    "decision_refs": ["objective"], "citations": [],
                },
                "policy": {
                    "editable_paths": [{"pattern": "solution/**", "origin": "generated"}],
                    "rationale": "Keep environment dynamics fixed.", "source": "human",
                    "decision_refs": ["policy_boundary"], "citations": [],
                },
                "environment_adapter": {
                    "owner": "subject", "source_path": "README.md",
                    "entrypoint": "demo:Environment", "interface": "Python callable",
                    "rationale": "Use the documented interface.", **provenance,
                },
                "outcome": {
                    "statistic": "expected score", "direction": "higher", "unit": "score",
                    "aggregation": "paired mean", "extraction": "subject result score",
                    "result_path": ["score"],
                    "source": "human", "decision_refs": ["outcome"], "citations": [],
                },
                "trial": {
                    "unit": "one generated map", "termination": "map completion",
                    "horizon": {"unit": "actions", "limit": 1000, "case_field": "max_actions"},
                    "seed_handling": "seed initializes the map generator", **provenance,
                },
                "derived_setup": {
                    "hard_rules": ["Do not edit environment dynamics."],
                    "hidden_data": "Seeds and scoring remain evaluator-private.",
                    "telemetry": [], "runtime_limits": ["60 seconds per process"],
                    "evaluator_pattern": "paired comparison",
                },
                "conformance": {
                    "seeded_variation": True,
                    "arm_symmetry": "not_applicable",
                    "arm_symmetry_rationale": "The fixture does not provide an alternate output.",
                },
                "direct_dependencies": [],
            },
        }

    def write_authorized_design(self, dependencies: list[dict]) -> dict:
        design = self.design_batch()["design"]
        design.update(
            {
                "schema_version": 2,
                "revision": 1,
                "decision_revision": 1,
                "source_provenance": {
                    "path": str(self.source),
                    "commit": self.record["source_commit"],
                },
                "controller_contract": {
                    "version": 1,
                    "sha256": hashlib.sha256(
                        json.dumps(
                            SETUP_CONTROLLER_CONTRACT,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                },
                "dependency_source_policy": {
                    "index": "https://pypi.org/simple",
                    "fingerprint": hashlib.sha256(
                        b"https://pypi.org/simple"
                    ).hexdigest(),
                },
                "direct_dependencies": dependencies,
            }
        )
        atomic_write_json(
            self.directory / "setup" / "authorized-design.public.json", design
        )
        atomic_write_json(
            self.directory / "setup" / "authorization.public.json",
            {
                "schema_version": 1,
                "authorized": True,
                "design_sha256": hashlib.sha256(
                    json.dumps(design, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "decision_revision": design["decision_revision"],
            },
        )
        decisions_path = self.directory / "setup" / "decisions.public.json"
        if not decisions_path.is_file():
            atomic_write_json(
                decisions_path,
                {
                    "schema_version": 1,
                    "revision": design["decision_revision"],
                    "decisions": [],
                },
            )
        return design

    def test_init_creates_output_free_workspace_and_location_safe_resume(self) -> None:
        self.assertEqual(self.record["setup_contract"], "conversation-v2")
        self.assertFalse((self.workspace / "ARCTL_SETUP.md").exists())
        payload = _init(self.source, self.root / "other-workspace", "other", None)
        self.assertIn("--data", payload["next_command"])
        self.assertIn("other-workspace/.arctl-data", payload["next_command"])

    def test_workspace_manifest_wins_over_unrelated_outer_git_config(self) -> None:
        nested = self.workspace / "environment"
        previous = Path.cwd()
        try:
            os.chdir(nested)
            self.assertEqual(_data_root(None), self.data.resolve())
        finally:
            os.chdir(previous)

    def test_legacy_setup_state_is_reported_without_mutation(self) -> None:
        setup_path = self.directory / "setup.json"
        legacy = json.loads(setup_path.read_text())
        legacy["schema_version"] = 1
        legacy.pop("setup_contract", None)
        atomic_write_json(setup_path, legacy)
        before = setup_path.read_bytes()
        with self.assertRaisesRegex(StateError, "legacy guided-setup state"):
            _setup(
                data_root=self.data,
                task_id="demo",
                answers_path=None,
                offline=True,
                acceptance=None,
                design_authorization=None,
                interactive=False,
            )
        self.assertEqual(setup_path.read_bytes(), before)

    def test_answers_replace_each_decision_and_reject_stale_or_partial_batches(self) -> None:
        batch = validate_batch(self.question_batch(), subject=self.subject, revision=1)
        save_batch(self.directory, batch)
        submission = {
            "revision": 1,
            "answers": {
                "objective": "objective_a",
                "outcome": {"custom": "Maximize cleared lines."},
                "policy_boundary": "policy_boundary_b",
            },
        }
        answer_batch(self.directory, self.record, submission)
        decisions = {item["id"]: item["answer"] for item in load_decisions(self.directory)["decisions"]}
        self.assertEqual(decisions["objective"], "First")
        self.assertEqual(decisions["outcome"], "Maximize cleared lines.")
        self.assertNotIn("Human clarification", json.dumps(decisions))
        with self.assertRaisesRegex(ValidationError, "stale"):
            answer_batch(self.directory, self.record, {**submission, "revision": 0})
        with self.assertRaisesRegex(ValidationError, "exactly"):
            answer_batch(
                self.directory,
                self.record,
                {"revision": 1, "answers": {"objective": "objective_a"}},
            )

    def test_complete_design_requires_human_owned_core_and_authorizes_by_hash(self) -> None:
        batch = validate_batch(self.question_batch(), subject=self.subject, revision=1)
        save_batch(self.directory, batch)
        answer_batch(
            self.directory,
            self.record,
            {
                "revision": 1,
                "answers": {
                    "objective": "objective_a",
                    "outcome": "outcome_a",
                    "policy_boundary": "policy_boundary_a",
                },
            },
        )
        _, record = load_setup(self.data, "demo")
        design = validate_batch(self.design_batch(), subject=self.subject, revision=2)
        token = finalize_design(
            self.directory,
            record,
            design,
            controller_contract=SETUP_CONTROLLER_CONTRACT,
        )
        _, record = load_setup(self.data, "demo")
        authorize_design(self.directory, record, token)
        _, record = load_setup(self.data, "demo")
        self.assertEqual(record["state"], "BUILD_REQUIRED")
        resolved = json.loads((self.directory / "setup" / "authorized-design.public.json").read_text())
        self.assertEqual(resolved["schema_version"], 2)
        note = render_setup_note(self.directory, record)
        self.assertIn("## Confirmed choices", note.read_text())
        self.assertIn("## Authorized setup", note.read_text())
        with self.assertRaisesRegex(StateError, "already exists"):
            render_setup_note(self.directory, record)

    def test_authorization_rejects_a_token_for_an_edited_design(self) -> None:
        batch = validate_batch(self.question_batch(), subject=self.subject, revision=1)
        save_batch(self.directory, batch)
        answer_batch(
            self.directory,
            self.record,
            {
                "revision": 1,
                "answers": {
                    "objective": "objective_a",
                    "outcome": "outcome_a",
                    "policy_boundary": "policy_boundary_a",
                },
            },
        )
        _, record = load_setup(self.data, "demo")
        design_batch = validate_batch(self.design_batch(), subject=self.subject, revision=2)
        token = finalize_design(
            self.directory,
            record,
            design_batch,
            controller_contract=SETUP_CONTROLLER_CONTRACT,
        )
        design_path = self.directory / "setup" / "design.public.json"
        edited = json.loads(design_path.read_text())
        edited["outcome"]["statistic"] = "different statistic"
        atomic_write_json(design_path, edited)
        _, record = load_setup(self.data, "demo")
        with self.assertRaisesRegex(ValidationError, "current design"):
            authorize_design(self.directory, record, token)
        self.assertFalse(
            (self.directory / "setup" / "authorized-design.public.json").exists()
        )
        self.assertEqual(load_setup(self.data, "demo")[1]["state"], "DESIGN_AUTHORIZATION_REQUIRED")

    def test_interactive_options_show_the_exact_untruncated_value(self) -> None:
        batch = self.question_batch()
        exact = "canonical-" + "x" * 400
        batch["questions"] = [batch["questions"][0]]
        batch["questions"][0]["options"][0]["value"] = exact
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("builtins.input", return_value="1"):
            submission = _print_question_batch(batch)
        self.assertIn("Will save (JSON): " + json.dumps(exact), output.getvalue())
        self.assertEqual(submission["answers"]["objective"], "objective_a")

    def test_special_dependency_source_requires_a_separate_human_decision(self) -> None:
        design = self.write_authorized_design(
            [
                    {
                        "requirement": "demo @ https://example.invalid/demo.whl",
                        "reason": "Fixture dependency.",
                        "origin": "proposed",
                        "authorization_decision": "dependency_source_demo",
                    }
                ]
        )
        with self.assertRaisesRegex(ValidationError, "special source"):
            _validate_dependency_plan(
                ["demo @ https://example.invalid/demo.whl"],
                directory=self.directory,
            )
        atomic_write_json(
            self.directory / "setup" / "decisions.public.json",
            {
                "schema_version": 1,
                "revision": 1,
                "decisions": [
                    {
                        "id": "dependency_source_demo",
                        "question": "Allow direct source?",
                        "answer": "Allow this exact URL.",
                        "option_id": "allow",
                        "source": "human",
                        "citations": [],
                    }
                ],
            },
        )
        _validate_dependency_plan(
            ["demo @ https://example.invalid/demo.whl"],
            directory=self.directory,
        )

    def test_late_direct_dependency_reopens_the_decision_flow_before_install(self) -> None:
        self.write_authorized_design([])
        with self.assertRaisesRegex(StateError, "explicit decision"):
            _validate_dependency_plan(["numpy>=2"], directory=self.directory)
        _, record = load_setup(self.data, "demo")
        self.assertEqual(record["state"], "DISCOVERY_REQUIRED")
        self.assertEqual(record["late_dependencies"], ["numpy>=2"])

    def test_direct_builder_writes_staging_and_returns_only_a_compact_report(self) -> None:
        atomic_write_json(
            self.directory / "setup" / "authorized-design.public.json",
            self.write_authorized_design([]),
        )
        captured: dict[str, object] = {}

        def fake_agent(**kwargs):
            captured.update(kwargs)
            staging = kwargs["worktree"]
            subject = staging / "subject"
            evaluator = staging / "evaluator"
            environment = staging / "environment"
            (subject / "_arctl").mkdir()
            (subject / "_arctl" / "hook.py").write_text("def run_batch(value): return value\n")
            (evaluator / "_arctl").mkdir()
            (evaluator / "_arctl" / "hook.py").write_text("# evaluator hooks\n")
            (evaluator / "test_generated_evaluator.py").write_text("# tests\n")
            (environment / "README.md").write_text("Environment\n")
            (staging / "task.design.json").write_text("{}")
            (staging / "evaluator.design.json").write_text("{}")
            return {
                "schema_version": 1,
                "summary": "Compact build.",
                "dependencies": [],
                "subject_files": [],
                "environment_files": ["README.md"],
                "evaluator_files": [],
            }

        with patch("arctl.setup._agent_run", side_effect=fake_agent), patch(
            "arctl.setup.build_setup", return_value={"review": "ready"}
        ):
            result = build_setup_direct(
                self.directory,
                self.record,
                offline=False,
            )
        self.assertEqual(result["review"], "ready")
        self.assertTrue(captured["writable_worktree"])
        self.assertTrue(captured["offline"])
        self.assertEqual(captured["output_name"], "build-report.public.json")
        self.assertIn('"conformance"', captured["prompt"])
        self.assertIn('"direct_dependencies"', captured["prompt"])
        self.assertIn('"horizon"', captured["prompt"])
        pending = json.loads((self.directory / "setup.json").read_text())["pending_build"]
        internal = json.loads(Path(pending["output"]).read_text())
        self.assertEqual(internal["environment_files"][0]["content"], "Environment\n")

    def test_clean_review_requires_complete_cited_coverage(self) -> None:
        areas = (
            "intent_fidelity", "grounding", "editable_boundary", "dependencies",
            "trial_independence", "scoring_statistics", "seed_handling",
            "runtime_behavior",
        )
        review = {
            "coverage": {
                area: {
                    "status": "pass",
                    "summary": "Inspected.",
                    "evidence": [{
                        "path": "README.md",
                        "location": "line 1",
                        "finding": "The public repository was inspected.",
                    }],
                }
                for area in areas
            },
            "findings": [],
        }
        _validate_review_evidence(review, roots=(self.subject,))
        review["coverage"]["seed_handling"]["evidence"] = []
        with self.assertRaisesRegex(ValidationError, "requires evidence"):
            _validate_review_evidence(review, roots=(self.subject,))


if __name__ == "__main__":
    unittest.main()
