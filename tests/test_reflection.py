from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arctl.errors import StateError
from arctl.manifest import EvaluatorManifest
from arctl.models import TaskConfig
from arctl.reflection import run_reflection, validate_reflection
from arctl.sandbox import MAX_AGENT_PROMPT_BYTES
from arctl.search import add_ledger_entry

from .helpers import valid_task
from .test_manifest import valid_manifest


def assessment(metrics: list[str]) -> dict:
    return {
        "summary": "The mechanism remains unproven.",
        "strategy_behavior": {
            "id": "avoid-errors",
            "realization": "unclear",
            "evidence": [],
        },
        "mechanism": {
            "status": "not_demonstrated",
            "evidence": [],
            "missing_evidence": ["Direct activation telemetry is absent."],
        },
        "implementation": {"status": "no_specific_concern", "concerns": []},
        "material_signals": [
            {
                "metric": name,
                "finding": "inconclusive",
                "interpretation": "The metric does not isolate the mechanism.",
            }
            for name in metrics
        ],
        "history_citations": [],
        "policy_observations": [],
        "next_action": {
            "kind": "revisit_after_better_evidence",
            "rationale": "Activation was not measured.",
            "test": None,
        },
    }


class ReflectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.experiment = self.root / "task" / "experiments" / "000001"
        self.candidate = self.root / "candidate"
        self.champion = self.root / "champion"
        for path in (self.experiment, self.candidate, self.champion):
            path.mkdir(parents=True)
        raw_task = valid_task()
        raw_task["repo"] = str(self.root / "subject")
        self.task = TaskConfig.from_mapping(raw_task)
        self.manifest = EvaluatorManifest.from_mapping(
            valid_manifest(telemetry=True)
        )
        self.request = {
            "strategy_behavior_id": "avoid-errors",
            "claim": "Reduce errors.",
            "mechanism": "Prefer safe actions.",
            "viability": "The policy can distinguish safe actions.",
            "evidence_review": {"summary": "No prior evidence.", "citations": []},
            "lineage": {"kind": "new", "prior_entry_id": None},
            "expected_effect": "Increase the primary score.",
            "expected_telemetry": {"errors": "decrease"},
            "falsifiers": ["Errors do not decrease."],
        }
        self.result = {
            "experiment_id": 1,
            "decision": "ARCHIVE",
            "evaluation": {
                "statistic": "expected score",
                "comparisons": [
                    {
                        "kind": "primary",
                        "trials": 64,
                        "effect_estimate": 0.2,
                        "one_sided_lower_bound": -0.5,
                        "suspect_test_required": False,
                        "suspect_test_reason": None,
                    }
                ],
            },
            "telemetry": {
                "errors": {"champion": 3.0, "candidate": 2.5, "delta": -0.5}
            },
            "constraints": {"tests": "PASS"},
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def builder(value: dict):
        def command(_worktree, scratch, _schema, _prompt):
            script = (
                "import pathlib,sys;"
                "pathlib.Path(sys.argv[1]).write_text(sys.argv[2])"
            )
            return (
                "python3",
                "-c",
                script,
                str(scratch / "assessment.public.json"),
                json.dumps(value),
            )

        return command

    def reflect(self, *, manifest=None, command_builder=None):
        return run_reflection(
            task=self.task,
            experiment=self.experiment,
            manifest=manifest or self.manifest,
            request=self.request,
            result=self.result,
            candidate_worktree=self.candidate,
            champion_worktree=self.champion,
            stop_path=self.root / "stop",
            command_builder=command_builder,
        )

    def test_saves_grounded_assessment_and_uncertainty_margin(self) -> None:
        value = self.reflect(command_builder=self.builder(assessment([])))

        self.assertEqual(value["status"], "COMPLETE")
        self.assertAlmostEqual(value["basis"]["uncertainty_margin"], 0.7)
        self.assertEqual(
            value["assessment"]["next_action"]["kind"],
            "revisit_after_better_evidence",
        )

    def test_malformed_saved_complete_reflection_has_a_domain_error(self) -> None:
        value = {
            "status": "COMPLETE",
            "warning": None,
            "basis": {"strategy_behavior_id": "avoid-errors"},
            "assessment": None,
        }

        with self.assertRaisesRegex(StateError, "saved complete reflection is invalid"):
            validate_reflection(value, metric_names=("errors",))

    def test_rejects_duplicate_or_unknown_material_signals(self) -> None:
        value = assessment(["errors", "errors"])
        wrapper = {
            "status": "COMPLETE",
            "warning": None,
            "basis": {"strategy_behavior_id": "avoid-errors"},
            "assessment": value,
        }
        with self.assertRaisesRegex(StateError, "unique declared metrics"):
            validate_reflection(wrapper, metric_names=("errors",))

        value["material_signals"] = [
            {
                "metric": "unknown",
                "finding": "anomalous",
                "interpretation": "Unexpected.",
            }
        ]
        with self.assertRaisesRegex(StateError, "assessment is invalid"):
            validate_reflection(wrapper, metric_names=("errors",))

    def test_saved_reflection_rejects_unknown_history_citations_from_its_basis(self) -> None:
        value = assessment(["errors"])
        value["history_citations"] = [
            {
                "entry_id": "unknown-entry",
                "bearing": "supports",
                "finding": "Dangling evidence.",
            }
        ]
        wrapper = {
            "status": "COMPLETE",
            "warning": None,
            "basis": {
                "strategy_behavior_id": "avoid-errors",
                "history_entry_ids": ["known-entry"],
            },
            "assessment": value,
        }

        with self.assertRaisesRegex(StateError, "unknown history"):
            validate_reflection(wrapper, metric_names=("errors",))

    def test_generated_schema_fixes_metrics_behavior_and_empty_history(self) -> None:
        seen = {}

        def builder(_worktree, scratch, schema, _prompt):
            seen.update(json.loads(schema.read_text()))
            value = assessment(["errors"])
            return self.builder(value)(_worktree, scratch, schema, _prompt)

        reflected = self.reflect(command_builder=builder)

        properties = seen["properties"]
        self.assertEqual(
            properties["strategy_behavior"]["properties"]["id"]["const"],
            "avoid-errors",
        )
        self.assertEqual(
            properties["material_signals"]["items"]["properties"]["metric"]["enum"],
            ["errors"],
        )
        self.assertEqual(properties["history_citations"]["maxItems"], 0)
        self.assertEqual(self.reflect(), reflected)

    def test_agent_preflight_failure_is_saved_at_the_attempt_boundary(self) -> None:
        raw_task = valid_task()
        raw_task["repo"] = str(self.root / "subject")
        self.task = TaskConfig.from_mapping(raw_task)

        with (
            mock.patch(
                "arctl.reflection.agent_command",
                side_effect=StateError("Codex output schema rejected"),
            ),
            self.assertRaisesRegex(StateError, "post-trial reflection failed"),
        ):
            self.reflect()

        failure = self.experiment / "reflection" / "attempts" / "0001" / "reflection.failure.json"
        self.assertTrue(failure.is_file())
        self.assertIn("Codex output schema rejected", failure.read_text())

    def test_reflection_cites_a_canonical_history_entry(self) -> None:
        entry = add_ledger_entry(
            self.experiment.parent.parent,
            {
                "source": "search:000001",
                "kind": "research_miss",
                "rejection_code": "duplicate_hypothesis",
                "message": "The same safe-action mechanism was already tested.",
            },
        )
        value = assessment(["errors"])
        value["history_citations"] = [
            {
                "entry_id": entry["entry_id"],
                "bearing": "supports",
                "finding": "The prior entry supports implementation feasibility.",
            },
        ]

        reflected = {
            "status": "COMPLETE",
            "warning": None,
            "basis": {
                "strategy_behavior_id": "avoid-errors",
                "history_entry_ids": [entry["entry_id"]],
            },
            "assessment": value,
        }
        validate_reflection(reflected, metric_names=("errors",))
        self.assertEqual(
            [
                citation["entry_id"]
                for citation in reflected["assessment"]["history_citations"]
            ],
            [entry["entry_id"]],
        )

    def test_prompt_size_does_not_grow_with_canonical_history(self) -> None:
        add_ledger_entry(
            self.experiment.parent.parent,
            {
                "source": "search:000001",
                "kind": "research_miss",
                "rejection_code": "diagnostic",
                "message": "short catalog summary",
                "planning": {"detail": "large history " * 20_000},
            },
        )
        seen_prompt = ""

        def builder(worktree, scratch, schema, prompt):
            nonlocal seen_prompt
            seen_prompt = prompt
            return self.builder(assessment([]))(worktree, scratch, schema, prompt)

        self.reflect(command_builder=builder)

        self.assertLess(len(seen_prompt.encode("utf-8")), MAX_AGENT_PROMPT_BYTES)
        self.assertNotIn("large history", seen_prompt)

    def test_failure_is_preserved_and_a_later_attempt_can_recover(self) -> None:
        with self.assertRaisesRegex(StateError, "post-trial reflection failed"):
            self.reflect(command_builder=self.builder(assessment(["errors"] * 6)))
        self.assertTrue(
            (
                self.experiment
                / "reflection"
                / "attempts"
                / "0001"
                / "reflection.failure.json"
            ).is_file()
        )

        recovered = self.reflect(
            command_builder=self.builder(assessment([]))
        )
        self.assertEqual(recovered["status"], "COMPLETE")
        self.assertTrue(
            (self.experiment / "reflection" / "attempts" / "0002").is_dir()
        )

    def test_reflection_must_assess_the_selected_strategy_behavior(self) -> None:
        value = assessment([])
        value["strategy_behavior"]["id"] = "different-behavior"
        with self.assertRaisesRegex(StateError, "post-trial reflection failed"):
            self.reflect(command_builder=self.builder(value))

    def test_empty_telemetry_contract_skips_model_with_warning(self) -> None:
        manifest = EvaluatorManifest.from_mapping(valid_manifest())
        called = False

        def forbidden(*_args):
            nonlocal called
            called = True
            return ("false",)

        value = self.reflect(manifest=manifest, command_builder=forbidden)
        self.assertEqual(value["status"], "SKIPPED_NO_TELEMETRY")
        self.assertFalse(called)
