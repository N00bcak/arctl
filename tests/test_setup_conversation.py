from __future__ import annotations

import json
import hashlib
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from arctl.cli import _data_root, _init, _print_question_batch, _setup
from arctl.codex_schema import validate_codex_output_schema
from arctl.errors import StateError, ValidationError
from arctl.setup import (
    QUESTION_IDS,
    SETUP_CONTROLLER_CONTRACT,
    _cross_artifact_findings,
    _declared_dependency_requirements,
    _direct_build_schema,
    _read_direct_build_files,
    _validate_dependency_plan,
    _validate_review_evidence,
    build_setup_direct,
    discover_setup_batch,
    initialize_setup,
    load_setup,
    reopen_review_decision_batch,
    review_schema,
)
from arctl.storage import atomic_write_json
from arctl.setup_protocol import UNITTEST_ENTRYPOINT
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
                    "excerpt_sha256": None,
                }
            ],
        }

    def question_batch(self, revision: int = 1) -> dict:
        questions = []
        for identifier in ("objective", "outcome", "policy_boundary"):
            options = [
                self.option(f"{identifier}_a", "First"),
                self.option(f"{identifier}_b", "Second"),
            ]
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
                    "value": "Improve the demo policy.",
                    "source": "human",
                    "decision_refs": ["objective"],
                    "citations": [],
                },
                "policy": {
                    "editable_paths": [
                        {"pattern": "solution/**", "origin": "generated"}
                    ],
                    "rationale": "Keep environment dynamics fixed.",
                    "source": "human",
                    "decision_refs": ["policy_boundary"],
                    "citations": [],
                },
                "environment_adapter": {
                    "owner": "subject",
                    "source_path": "README.md",
                    "entrypoint": "demo:Environment",
                    "interface": "Python callable",
                    "rationale": "Use the documented interface.",
                    **provenance,
                },
                "outcome": {
                    "statistic": "expected score",
                    "direction": "higher",
                    "unit": "score",
                    "aggregation": "paired mean",
                    "extraction": "subject result score",
                    "result_path": ["score"],
                    "source": "human",
                    "decision_refs": ["outcome"],
                    "citations": [],
                },
                "trial": {
                    "unit": "one generated map",
                    "termination": "map completion",
                    "horizon": {
                        "unit": "actions",
                        "limit": 1000,
                        "case_field": "max_actions",
                    },
                    "seed_handling": "seed initializes the map generator",
                    **provenance,
                },
                "derived_setup": {
                    "hard_rules": ["Do not edit environment dynamics."],
                    "hidden_data": "Seeds and scoring remain evaluator-private.",
                    "telemetry": [],
                    "runtime_limits": ["60 seconds per process"],
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
                preflight=False,
            )
        self.assertEqual(setup_path.read_bytes(), before)

    def test_setup_reports_an_already_accepted_setup_as_ready_for_approval(self) -> None:
        setup_path = self.directory / "setup.json"
        accepted = json.loads(setup_path.read_text())
        accepted["state"] = "READY_FOR_APPROVAL"
        atomic_write_json(setup_path, accepted)
        note = self.workspace / "ARCTL_SETUP.md"
        note.write_text("# Accepted setup\n", encoding="utf-8")

        payload = _setup(
            data_root=self.data,
            task_id="demo",
            answers_path=None,
            offline=True,
            acceptance=None,
            design_authorization=None,
            interactive=False,
            preflight=False,
        )

        self.assertTrue(payload["success"])
        self.assertEqual(payload["state"], "READY_FOR_APPROVAL")
        self.assertEqual(payload["allowed_actions"], ["approve"])
        self.assertEqual(payload["next_command"], "arctl approve demo")
        self.assertEqual(Path(payload["artifacts"][0]["path"]), note.resolve())
        self.assertEqual(load_setup(self.data, "demo")[1]["state"], "READY_FOR_APPROVAL")

    def test_answers_replace_each_decision_and_reject_stale_or_partial_batches(
        self,
    ) -> None:
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
        decisions = {
            item["id"]: item["answer"]
            for item in load_decisions(self.directory)["decisions"]
        }
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

    def test_complete_design_requires_human_owned_core_and_authorizes_by_hash(
        self,
    ) -> None:
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
        resolved = json.loads(
            (self.directory / "setup" / "authorized-design.public.json").read_text()
        )
        self.assertEqual(resolved["schema_version"], 3)
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
        design_batch = validate_batch(
            self.design_batch(), subject=self.subject, revision=2
        )
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
        self.assertEqual(
            load_setup(self.data, "demo")[1]["state"], "DESIGN_AUTHORIZATION_REQUIRED"
        )

    def test_design_cannot_invent_dependency_authorization_decisions(self) -> None:
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
        batch = self.design_batch()
        batch["design"]["direct_dependencies"] = [
            {
                "requirement": "requests",
                "imports": ["requests"],
                "reason": "Proposed fixture dependency.",
                "origin": "proposed",
                "authorization_decision": "invented_dependency_decision",
            }
        ]
        design = validate_batch(batch, subject=self.subject, revision=2)
        with self.assertRaisesRegex(ValidationError, "unknown decision"):
            finalize_design(
                self.directory,
                record,
                design,
                controller_contract=SETUP_CONTROLLER_CONTRACT,
            )

    def test_interactive_options_show_the_exact_untruncated_value(self) -> None:
        batch = self.question_batch()
        exact = "canonical-" + "x" * 400
        batch["questions"] = [batch["questions"][0]]
        batch["questions"][0]["options"][0]["value"] = exact
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            patch("builtins.input", return_value="1"),
        ):
            submission = _print_question_batch(batch)
        self.assertIn("Will save (JSON): " + json.dumps(exact), output.getvalue())
        self.assertEqual(submission["answers"]["objective"], "objective_a")

    def test_special_dependency_source_requires_a_separate_human_decision(self) -> None:
        design = self.write_authorized_design(
            [
                {
                    "requirement": "demo @ https://example.invalid/demo.whl",
                    "imports": ["demo"],
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

    def test_late_direct_dependency_reopens_the_decision_flow_before_install(
        self,
    ) -> None:
        self.write_authorized_design([])
        with self.assertRaisesRegex(StateError, "explicit decision"):
            _validate_dependency_plan(["numpy>=2"], directory=self.directory)
        _, record = load_setup(self.data, "demo")
        self.assertEqual(record["state"], "DISCOVERY_REQUIRED")
        self.assertEqual(record["late_dependencies"], ["numpy>=2"])

    def test_discovery_binds_the_next_decision_revision_in_the_agent_schema(
        self,
    ) -> None:
        atomic_write_json(
            self.directory / "setup" / "decisions.public.json",
            {
                "schema_version": 1,
                "revision": 1,
                "decisions": [
                    {"id": "objective"},
                    {"id": "outcome"},
                    {"id": "policy_boundary"},
                ],
            },
        )
        captured: dict[str, object] = {}

        def fake_agent(**kwargs):
            captured.update(kwargs)
            raise StateError("stop after schema capture")

        with patch("arctl.setup._agent_run", side_effect=fake_agent):
            with self.assertRaisesRegex(StateError, "stop after schema capture"):
                discover_setup_batch(self.directory, self.record)

        schema = captured["schema_value"]
        self.assertEqual(
            schema["properties"]["revision"],
            {"type": "integer", "const": 2},
        )
        decision_refs = schema["properties"]["design"]["anyOf"][1]["properties"][
            "objective"
        ]["properties"]["decision_refs"]
        self.assertEqual(
            decision_refs["items"]["enum"],
            ["objective", "outcome", "policy_boundary"],
        )

    def test_controller_turns_unapproved_proposed_dependency_into_question(
        self,
    ) -> None:
        decisions = {
            "schema_version": 1,
            "revision": 1,
            "decisions": [
                {"id": "objective"},
                {"id": "outcome"},
                {"id": "policy_boundary"},
            ],
        }
        atomic_write_json(self.directory / "setup" / "decisions.public.json", decisions)
        batch = self.design_batch()
        batch["design"]["direct_dependencies"] = [
            {
                "requirement": "opencv-python-headless",
                "imports": ["cv2"],
                "reason": "The repository imports cv2 at module load.",
                "origin": "proposed",
                "authorization_decision": None,
            }
        ]

        with patch("arctl.setup._agent_run", return_value=batch):
            result = discover_setup_batch(self.directory, self.record)

        self.assertIsNone(result["design"])
        self.assertEqual(len(result["questions"]), 1)
        question = result["questions"][0]
        self.assertEqual(question["id"], "allow_dependency_opencv_python_headless")
        self.assertEqual(question["recommended_option_id"], "allow")
        self.assertEqual(
            [item["id"] for item in question["options"]], ["allow", "reject"]
        )
        _, saved = load_setup(self.data, "demo")
        self.assertEqual(saved["state"], "QUESTIONS_REQUIRED")

    def test_review_conflict_reopens_and_replaces_objective_decision(self) -> None:
        design = self.write_authorized_design([])
        citation = self.option("grounded", "Grounded")["citations"][0]
        design["objective"]["citations"] = [citation]
        design["outcome"]["citations"] = [citation]
        atomic_write_json(
            self.directory / "setup" / "authorized-design.public.json", design
        )
        atomic_write_json(
            self.directory / "setup" / "decisions.public.json",
            {
                "schema_version": 1,
                "revision": 1,
                "decisions": [
                    {"id": "objective", "answer": "Maximize reward."},
                    {"id": "outcome", "answer": "Total lines cleared."},
                    {"id": "policy_boundary", "answer": "Edit policy only."},
                ],
            },
        )
        self.record["state"] = "REVIEW_FAILED"
        self.record["prior_review_findings"] = [
            {"code": "INTENT_OBJECTIVE_OUTCOME_MISMATCH"}
        ]

        batch = reopen_review_decision_batch(self.directory, self.record)

        self.assertIsNotNone(batch)
        assert batch is not None
        self.assertEqual(batch["questions"][0]["id"], "objective")
        _, saved = load_setup(self.data, "demo")
        self.assertEqual(saved["state"], "QUESTIONS_REQUIRED")
        decisions = answer_batch(
            self.directory,
            saved,
            {"revision": 2, "answers": {"objective": "total_lines_cleared"}},
        )
        objective = next(
            item for item in decisions["decisions"] if item["id"] == "objective"
        )
        self.assertEqual(
            objective["answer"],
            "Maximize total lines cleared per seeded episode over at most 1000 block placements.",
        )

    def test_review_conflict_reopens_seed_isolation_decision(self) -> None:
        self.write_authorized_design([])
        atomic_write_json(
            self.directory / "setup" / "decisions.public.json",
            {
                "schema_version": 1,
                "revision": 1,
                "decisions": [
                    {"id": "objective", "answer": "Maximize cleared lines."},
                    {"id": "outcome", "answer": "Total lines cleared."},
                    {"id": "policy_boundary", "answer": "Edit policy only."},
                ],
            },
        )
        self.record["state"] = "REVIEW_FAILED"
        self.record["prior_review_findings"] = [
            {"code": "AUTHORIZED_ADAPTER_CONTRACT_MISMATCH", "message": "stale"},
            {"code": "AUTHORIZED_TELEMETRY_CONTRACT_MISMATCH", "message": "stale"},
        ]

        batch = reopen_review_decision_batch(self.directory, self.record)

        self.assertIsNotNone(batch)
        assert batch is not None
        question = batch["questions"][0]
        self.assertEqual(question["id"], "seed_isolation")
        self.assertEqual(
            question["recommended_option_id"], "secure_seedless_subprocess"
        )
        _, saved = load_setup(self.data, "demo")
        self.assertEqual(saved["state"], "QUESTIONS_REQUIRED")
        self.assertIn(
            "AUTHORIZED_ADAPTER_CONTRACT_MISMATCH",
            " ".join(saved["prior_design_findings"]),
        )
        decisions = answer_batch(
            self.directory,
            saved,
            {
                "revision": 2,
                "answers": {"seed_isolation": "secure_seedless_subprocess"},
            },
        )
        isolation = next(
            item for item in decisions["decisions"] if item["id"] == "seed_isolation"
        )
        self.assertIn("separate seedless subprocess", isolation["answer"])

    def test_existing_seed_isolation_decision_dismisses_redundant_reopen(self) -> None:
        self.write_authorized_design([])
        atomic_write_json(
            self.directory / "setup" / "decisions.public.json",
            {
                "schema_version": 1,
                "revision": 2,
                "decisions": [
                    {"id": "seed_isolation", "answer": "Use a seedless process."}
                ],
            },
        )
        redundant = {
            "schema_version": 1,
            "revision": 3,
            "summary": "Redundant question.",
            "questions": [
                {
                    "id": "seed_isolation",
                    "prompt": "Repeat the seed decision?",
                    "why": "A broad review code was repeated.",
                    "options": [
                        {
                            "id": "secure",
                            "label": "Secure",
                            "value": "Use a seedless process.",
                            "consequence": "Preserve isolation.",
                            "citations": [
                                {
                                    "kind": "controller",
                                    "rule_id": "seeds",
                                    "finding": "Seeds remain hidden.",
                                }
                            ],
                        },
                        {
                            "id": "insecure",
                            "label": "Insecure",
                            "value": "Expose the seed.",
                            "consequence": "Lose isolation.",
                            "citations": [
                                {
                                    "kind": "controller",
                                    "rule_id": "seeds",
                                    "finding": "Seeds remain hidden.",
                                }
                            ],
                        },
                    ],
                    "recommended_option_id": "secure",
                    "allow_custom": True,
                }
            ],
            "design": None,
        }
        save_batch(self.directory, redundant)
        self.record["state"] = "QUESTIONS_REQUIRED"
        atomic_write_json(self.directory / "setup.json", self.record)

        migrated = reopen_review_decision_batch(self.directory, self.record)

        self.assertIsNotNone(migrated)
        self.assertEqual(load_setup(self.data, "demo")[1]["state"], "REVIEW_FAILED")

    def test_reauthorization_archives_previous_signed_design_and_resets_build_state(
        self,
    ) -> None:
        old_design = self.write_authorized_design([])
        old_authorization = json.loads(
            (self.directory / "setup" / "authorization.public.json").read_text()
        )
        atomic_write_json(
            self.directory / "setup" / "decisions.public.json",
            {
                "schema_version": 1,
                "revision": 2,
                "decisions": [
                    {"id": "objective", "answer": "Maximize cleared lines."},
                    {"id": "outcome", "answer": "Total lines cleared."},
                    {"id": "policy_boundary", "answer": "Edit policy only."},
                    {"id": "seed_isolation", "answer": "Use a seedless process."},
                ],
            },
        )
        revised = self.design_batch(revision=3)
        revised["design"]["summary"] = "Reauthorized seedless design."
        token = finalize_design(
            self.directory,
            self.record,
            validate_batch(revised, subject=self.subject, revision=3),
            controller_contract=SETUP_CONTROLLER_CONTRACT,
        )
        _, awaiting = load_setup(self.data, "demo")
        for key in (
            "pending_build",
            "prior_build_findings",
            "prior_review_findings",
            "behavior_repair_attempted_for",
            "behavior_repair_completed_for",
            "clean_review",
            "acceptance_token",
        ):
            awaiting[key] = "stale"
        atomic_write_json(self.directory / "setup.json", awaiting)

        authorize_design(self.directory, awaiting, token)

        history = list(
            (self.directory / "setup" / "authorization-history").glob("*")
        )
        self.assertEqual(len(history), 1)
        self.assertEqual(
            json.loads((history[0] / "authorized-design.public.json").read_text()),
            old_design,
        )
        self.assertEqual(
            json.loads((history[0] / "authorization.public.json").read_text()),
            old_authorization,
        )
        _, saved = load_setup(self.data, "demo")
        self.assertEqual(saved["state"], "BUILD_REQUIRED")
        for key in (
            "pending_build",
            "prior_build_findings",
            "prior_review_findings",
            "behavior_repair_attempted_for",
            "behavior_repair_completed_for",
            "clean_review",
            "acceptance_token",
        ):
            self.assertNotIn(key, saved)

    def test_direct_builder_stages_code_and_returns_typed_designs(self) -> None:
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
            (subject / "_arctl" / "hook.py").write_text(
                "def run_batch(value): return value\n"
            )
            (subject / "_arctl" / "public_probe.py").write_text(
                "from _arctl.subject import main\n"
            )
            (evaluator / "_arctl").mkdir()
            (evaluator / "_arctl" / "hook.py").write_text("# evaluator hooks\n")
            (evaluator / "test_generated_evaluator.py").write_text("# tests\n")
            return {
                "schema_version": 3,
                "summary": "Compact build.",
                "files": [
                    {
                        "repository": "subject",
                        "path": "_arctl/hook.py",
                        "role": "subject_hook",
                    },
                    {
                        "repository": "subject",
                        "path": "_arctl/public_probe.py",
                        "role": "public_probe",
                    },
                    {
                        "repository": "evaluator",
                        "path": "_arctl/hook.py",
                        "role": "evaluator_hook",
                    },
                    {
                        "repository": "evaluator",
                        "path": "test_generated_evaluator.py",
                        "role": "evaluator_test",
                    },
                ],
                "task": {
                    "objective": "Model echo.",
                    "editable_paths": ["wrong/**"],
                    "public_probe": {
                        "execution": {
                            "kind": "module",
                            "target": "_arctl.public_probe",
                            "arguments": [],
                        },
                        "trial_equivalents": 1,
                    },
                    "environment": {
                        "codebases": [
                            {
                                "id": "subject-interface",
                                "description": "Subject interface.",
                                "owner": "subject",
                                "include": [],
                            },
                            {
                                "id": "unused-environment",
                                "description": "No external environment files are used.",
                                "owner": "environment",
                                "include": [],
                            },
                        ],
                        "probes": [],
                    },
                },
                "evaluator": {
                    "public": {
                        "statistic": "Model echo.",
                        "telemetry": [
                            {
                                "name": "mean_score",
                                "description": "Mean score by arm.",
                                "unit": "score",
                                "scope": "paired",
                                "role": "outcome",
                                "value_type": "number",
                                "direction": "higher",
                            },
                            {
                                "name": "cap_rate",
                                "description": "Fraction reaching the cap.",
                                "unit": "fraction",
                                "scope": "paired",
                                "role": "implementation",
                                "value_type": "number",
                                "direction": "contextual",
                            },
                        ],
                    },
                    "trial": {
                        "meaning": "Model echo.",
                        "seed_to_case": "Model echo.",
                    },
                    "schemas": {
                        "public_case_json": json.dumps(
                            {
                                "type": "object",
                                "properties": {"value": {"type": "integer"}},
                                "required": ["value"],
                                "additionalProperties": False,
                            }
                        ),
                        "subject_result_json": json.dumps(
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["case_id", "score", "telemetry"],
                                "properties": {
                                    "case_id": {"type": "string"},
                                    "score": {"type": "integer", "minimum": 0},
                                    "telemetry": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": ["decisions", "capped"],
                                        "properties": {
                                            "decisions": {"type": "integer"},
                                            "capped": {"type": "boolean"},
                                        },
                                    },
                                },
                            }
                        ),
                    },
                    "setup_contract": {},
                },
            }

        with (
            patch("arctl.setup._agent_run", side_effect=fake_agent),
            patch("arctl.setup.build_setup", return_value={"review": "ready"}),
        ):
            result = build_setup_direct(
                self.directory,
                self.record,
                offline=False,
            )
        self.assertEqual(result["review"], "ready")
        self.assertTrue(captured["writable_worktree"])
        self.assertTrue(captured["offline"])
        self.assertTrue(callable(captured["normalize_output"]))
        self.assertEqual(captured["output_name"], "build-report.public.json")
        self.assertEqual(
            captured["schema_value"]["properties"]["schema_version"],
            {"type": "integer", "const": 3},
        )
        self.assertIn("task", captured["schema_value"]["properties"])
        self.assertIn("evaluator", captured["schema_value"]["properties"])
        task_schema = captured["schema_value"]["properties"]["task"]["properties"]
        evaluator_schema = captured["schema_value"]["properties"]["evaluator"][
            "properties"
        ]
        self.assertEqual(task_schema["objective"]["const"], "Improve the demo policy.")
        self.assertEqual(task_schema["editable_paths"]["minItems"], 1)
        self.assertEqual(task_schema["editable_paths"]["maxItems"], 1)
        self.assertEqual(
            task_schema["editable_paths"]["items"]["enum"], ["solution/**"]
        )
        self.assertEqual(
            evaluator_schema["public"]["properties"]["statistic"]["const"],
            "expected score",
        )
        hard_rules = evaluator_schema["setup_contract"]["properties"]["hard_rules"]
        self.assertEqual(hard_rules["minItems"], 1)
        self.assertEqual(
            hard_rules["items"]["enum"], ["Do not edit environment dynamics."]
        )
        self.assertIn("Do not write", captured["prompt"])
        self.assertIn("files at the staging root", captured["prompt"])
        self.assertIn('"conformance"', captured["prompt"])
        self.assertIn('"direct_dependencies"', captured["prompt"])
        self.assertIn('"horizon"', captured["prompt"])
        pending = json.loads((self.directory / "setup.json").read_text())[
            "pending_build"
        ]
        internal = json.loads(Path(pending["output"]).read_text())
        self.assertEqual(internal["environment_files"], [])
        self.assertEqual(internal["task"]["objective"], "Improve the demo policy.")
        self.assertEqual(internal["task"]["editable_paths"], ["solution/**"])
        self.assertIn(
            "README.md",
            internal["task"]["environment"]["codebases"][0]["include"],
        )
        self.assertEqual(
            [
                source["id"]
                for source in internal["task"]["environment"]["codebases"]
            ],
            ["subject-interface"],
        )
        self.assertEqual(internal["evaluator"]["public"]["statistic"], "expected score")
        self.assertEqual(
            internal["evaluator"]["setup_contract"]["hard_rules"],
            ["Do not edit environment dynamics."],
        )
        public_case = json.loads(internal["evaluator"]["schemas"]["public_case_json"])
        self.assertEqual(
            public_case["properties"]["max_actions"],
            {"type": "integer", "const": 1000},
        )
        self.assertEqual(
            public_case["required"],
            ["case_id", "seed", "policy_path", "max_actions"],
        )
        self.assertEqual(
            public_case["properties"]["policy_path"],
            {"type": "string", "const": "solution/**"},
        )
        subject_result = json.loads(
            internal["evaluator"]["schemas"]["subject_result_json"]
        )
        self.assertEqual(subject_result["required"], ["case_id", "score", "telemetry"])
        self.assertEqual(
            subject_result["properties"]["score"],
            {"type": "integer", "minimum": 0},
        )
        self.assertEqual(
            subject_result["properties"]["telemetry"]["required"],
            ["decisions", "capped"],
        )
        self.assertEqual(
            internal["evaluator"]["public"]["subject_interface"],
            "Python callable",
        )
        self.assertEqual(
            [item["name"] for item in internal["evaluator"]["public"]["telemetry"]],
            ["mean_score", "cap_rate"],
        )

    def test_direct_builder_rejects_duplicate_fixed_file_declarations(self) -> None:
        roots = {
            name: self.root / "direct-files" / name
            for name in ("subject", "environment", "evaluator")
        }
        for root in roots.values():
            root.mkdir(parents=True)
        (roots["subject"] / "_arctl").mkdir()
        (roots["subject"] / "_arctl" / "hook.py").write_text("# subject\n")
        (roots["evaluator"] / "_arctl").mkdir()
        (roots["evaluator"] / "_arctl" / "hook.py").write_text("# evaluator\n")
        (roots["evaluator"] / "test_generated_evaluator.py").write_text("# test\n")
        declarations = [
            {"repository": "subject", "path": "_arctl/hook.py", "role": "subject_hook"},
            {
                "repository": "evaluator",
                "path": "_arctl/hook.py",
                "role": "evaluator_hook",
            },
            {
                "repository": "evaluator",
                "path": "test_generated_evaluator.py",
                "role": "evaluator_test",
            },
            {
                "repository": "evaluator",
                "path": "test_generated_evaluator.py",
                "role": "support",
            },
        ]
        with self.assertRaisesRegex(ValidationError, "OWN_DUPLICATE_PATH"):
            _read_direct_build_files(roots, declarations)

    def test_direct_builder_schema_rejects_repository_prefixed_paths(self) -> None:
        schema = _direct_build_schema()
        validate_codex_output_schema(schema)
        declaration = schema["properties"]["files"]["items"]
        validator = Draft202012Validator(declaration)
        for value in (
            {
                "repository": "subject",
                "path": "subject/_arctl/hook.py",
                "role": "subject_hook",
            },
        ):
            with self.assertRaises(JsonSchemaValidationError):
                validator.validate(value)
        validator.validate(
            {
                "repository": "subject",
                "path": "_arctl/hook.py",
                "role": "subject_hook",
            }
        )

    def test_dependency_validation_reports_prose_and_local_source_together(
        self,
    ) -> None:
        local = self.subject / "local_demo"
        local.mkdir()
        (local / "__init__.py").write_text("")
        design = self.design_batch()["design"]
        design["direct_dependencies"] = [
            {
                "requirement": "numpy (runtime dependency)",
                "imports": ["numpy"],
                "reason": "Fixture prose.",
                "origin": "repository",
                "authorization_decision": None,
            },
            {
                "requirement": "local_demo",
                "imports": ["local_demo"],
                "reason": "Local fixture.",
                "origin": "repository",
                "authorization_decision": None,
            },
        ]
        with self.assertRaises(ValidationError) as caught:
            _declared_dependency_requirements(design, subject=self.subject)
        self.assertIn("not valid PEP 508", str(caught.exception))
        self.assertIn("supplied by the subject tree", str(caught.exception))

    def test_legacy_dependency_design_preserves_decisions_and_reopens_discovery(
        self,
    ) -> None:
        self.write_authorized_design(
            [
                {
                    "requirement": "numpy (runtime dependency)",
                    "reason": "Legacy prose.",
                    "origin": "repository",
                    "authorization_decision": None,
                }
            ]
        )
        decisions_path = self.directory / "setup" / "decisions.public.json"
        before = decisions_path.read_bytes()
        with patch("arctl.setup._agent_run") as agent:
            with self.assertRaisesRegex(StateError, "deterministic validation"):
                build_setup_direct(self.directory, self.record, offline=False)
        agent.assert_not_called()
        _, saved = load_setup(self.data, "demo")
        self.assertEqual(saved["state"], "DISCOVERY_REQUIRED")
        self.assertEqual(decisions_path.read_bytes(), before)
        self.assertIn("DESIGN_DEPENDENCY", saved["prior_design_findings"][0])

    def test_invalid_cross_artifact_fixture_is_portable(self) -> None:
        fixture = (
            Path(__file__).parent / "fixtures" / "setup" / "invalid_cross_artifact"
        )
        for path in fixture.rglob("*"):
            self.assertFalse(path.is_symlink(), path)
            self.assertNotIn("__pycache__", path.parts)
            self.assertNotEqual(path.suffix, ".pyc")
            if path.is_file():
                content = path.read_text(encoding="utf-8")
                self.assertNotIn("/home/", content)
                self.assertNotIn("test_tris", content)

    def test_invalid_cross_artifact_fixture_reports_complete_failure_set(self) -> None:
        """Preserve the mechanical regressions reproduced from setup attempt 0006."""
        fixture = (
            Path(__file__).parent / "fixtures" / "setup" / "invalid_cross_artifact"
        )
        report = json.loads((fixture / "build-report.public.json").read_text())
        design = json.loads((fixture / "authorized-design.public.json").read_text())
        staging = self.root / "invalid-cross-artifact"
        shutil.copytree(fixture / "staging", staging)
        roots = {
            name: staging / name for name in ("subject", "environment", "evaluator")
        }
        declarations = [
            {"repository": "subject", "path": "_arctl/hook.py", "role": "subject_hook"},
            {
                "repository": "evaluator",
                "path": "_arctl/hook.py",
                "role": "evaluator_hook",
            },
            {
                "repository": "evaluator",
                "path": "test_generated_evaluator.py",
                "role": "evaluator_test",
            },
            *(
                {"repository": "subject", "path": path, "role": "support"}
                for path in report["subject_files"]
            ),
            *(
                {"repository": "environment", "path": path, "role": "support"}
                for path in report["environment_files"]
            ),
            *(
                {"repository": "evaluator", "path": path, "role": "support"}
                for path in report["evaluator_files"]
            ),
        ]
        with self.assertRaisesRegex(ValidationError, "OWN_DUPLICATE_PATH"):
            _read_direct_build_files(roots, declarations)
        with self.assertRaises(ValidationError) as dependency_error:
            _declared_dependency_requirements(design, subject=roots["subject"])
        self.assertIn("not valid PEP 508", str(dependency_error.exception))
        self.assertIn("supplied by the subject tree", str(dependency_error.exception))
        first = _cross_artifact_findings(
            report, design, subject=roots["subject"], environment=roots["environment"]
        )
        second = _cross_artifact_findings(
            report, design, subject=roots["subject"], environment=roots["environment"]
        )
        self.assertEqual(first, second)
        joined = "\n".join(first)
        for code in (
            "COMMAND_UNREACHABLE",
            "IMPORT_UNDECLARED adapter",
            "IMPORT_UNDECLARED cv2",
            "SOURCE_EDITABLE_OVERLAP",
            "SOURCE_UNREACHABLE",
        ):
            self.assertIn(code, joined)
        runner = self.root / "unittest_runner.py"
        runner.write_text(UNITTEST_ENTRYPOINT, encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-B", str(runner), str(roots["evaluator"])],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("skipped", completed.stderr)

    def test_design_rejects_adapter_source_path_with_appended_prose(self) -> None:
        batch = self.design_batch()
        batch["design"]["environment_adapter"][
            "source_path"
        ] = "README.md (subject-owned adapter)"
        with self.assertRaisesRegex(ValidationError, "must be one file path"):
            validate_batch(batch, subject=self.subject, revision=2)

    def test_design_requires_headless_opencv_for_cv2(self) -> None:
        batch = self.design_batch()
        batch["design"]["direct_dependencies"] = [
            {
                "requirement": "opencv-python",
                "imports": ["cv2"],
                "reason": "The repository imports cv2.",
                "origin": "repository",
                "authorization_decision": None,
            }
        ]
        with self.assertRaisesRegex(ValidationError, "opencv-python-headless"):
            validate_batch(batch, subject=self.subject, revision=2)

    def test_design_canonicalizes_equivalent_pep508_specifier_order(self) -> None:
        batch = self.design_batch()
        batch["design"]["direct_dependencies"] = [
            {
                "requirement": "gymnasium>=0.28.1,<2",
                "imports": ["gymnasium"],
                "reason": "The repository imports Gymnasium.",
                "origin": "repository",
                "authorization_decision": None,
            }
        ]

        normalized = validate_batch(batch, subject=self.subject, revision=2)

        self.assertEqual(
            normalized["design"]["direct_dependencies"][0]["requirement"],
            "gymnasium<2,>=0.28.1",
        )

    def test_clean_review_requires_complete_cited_coverage(self) -> None:
        areas = (
            "intent_fidelity",
            "grounding",
            "editable_boundary",
            "dependencies",
            "trial_independence",
            "scoring_statistics",
            "seed_handling",
            "runtime_behavior",
        )
        review = {
            "coverage": {
                area: {
                    "status": "pass",
                    "summary": "Inspected.",
                    "evidence": [
                        {
                            "path": "README.md",
                            "location": "line 1",
                            "finding": "The public repository was inspected.",
                        }
                    ],
                }
                for area in areas
            },
            "findings": [],
        }
        _validate_review_evidence(review, roots=(self.subject,))
        setup_root = self.directory / "setup"
        setup_root.mkdir(exist_ok=True)
        (setup_root / "authorized-design.public.json").write_text("{}\n")
        review["coverage"]["grounding"]["evidence"] = [
            {
                "path": "authorized-design.public.json",
                "location": "line 1",
                "finding": "The signed design was inspected.",
            }
        ]
        _validate_review_evidence(review, roots=(self.subject, setup_root))
        review["coverage"]["editable_boundary"]["evidence"][0]["location"] = (
            "lines 1-999999"
        )
        with self.assertRaisesRegex(ValidationError, "citation is outside"):
            _validate_review_evidence(review, roots=(self.subject, setup_root))
        review["coverage"]["editable_boundary"]["evidence"][0]["location"] = (
            "line 1"
        )
        review["coverage"]["seed_handling"]["evidence"] = []
        with self.assertRaisesRegex(ValidationError, "requires evidence"):
            _validate_review_evidence(review, roots=(self.subject, setup_root))

    def test_review_schema_rejects_multi_range_citation_locations(self) -> None:
        citation = review_schema()["properties"]["coverage"]["properties"]["grounding"][
            "properties"
        ]["evidence"]["items"]
        validator = Draft202012Validator(citation)
        with self.assertRaises(JsonSchemaValidationError):
            validator.validate(
                {
                    "path": "README.md",
                    "location": "lines 5-14, 78-81",
                    "finding": "Two disjoint ranges were combined.",
                }
            )
        validator.validate(
            {
                "path": "README.md",
                "location": "lines 5-14",
                "finding": "One contiguous range.",
            }
        )


if __name__ == "__main__":
    unittest.main()
