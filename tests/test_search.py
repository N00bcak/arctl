from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from arctl.errors import ValidationError
from arctl.manifest import EvaluatorManifest
from arctl.search import (
    add_ledger_entry,
    planning_schema,
    rebuild_catalog,
    strategy_schema,
    validate_planning,
)

from .test_manifest import valid_manifest


def request(behavior: str, mechanism: str) -> dict:
    return {
        "schema_version": 2,
        "strategy_behavior_id": behavior,
        "claim": "The selected policy change improves the score.",
        "mechanism": mechanism,
        "viability": "The editable policy can express the change.",
        "evidence_review": {"summary": "No contrary evidence.", "citations": []},
        "expected_effect": "The paired score difference is positive.",
        "expected_telemetry": {},
        "falsifiers": ["The paired effect is not positive."],
        "lineage": {"kind": "new", "prior_entry_id": None},
    }


def direction(behavior: str, candidate: dict | None) -> dict:
    return {
        "strategy_behavior_id": behavior,
        "champion_assessment": "The champion leaves a material gap.",
        "remaining_gap": "The behavior can be expressed more effectively.",
        "disposition": "candidate" if candidate is not None else "exhausted",
        "request": candidate,
        "evidence": ["Public evidence supports this assessment."],
        "feasibility": "The change is feasible within editable paths.",
        "expected_value": "This is worth one controlled comparison.",
    }


class PlanningContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = EvaluatorManifest.from_mapping(valid_manifest())
        self.strategy = {
            "successful_policy_behaviors": [
                {"id": "behavior-a"},
                {"id": "behavior-b"},
            ]
        }

    def test_selection_returns_the_direction_owned_request(self) -> None:
        selected_request = request(
            "behavior-a",
            "Use four epochs with a fully specified cosine learning-rate schedule.",
        )
        value = {
            "schema_version": 2,
            "directions": [
                direction("behavior-a", selected_request),
                direction("behavior-b", request("behavior-b", "Use an ensemble.")),
            ],
            "selection_rationale": "Behavior A has the strongest evidence.",
            "selection": "behavior-a",
        }
        Draft202012Validator(planning_schema(self.manifest)).validate(value)

        selected = validate_planning(
            value,
            strategy=self.strategy,
            ledger=[],
            manifest=self.manifest,
        )

        self.assertEqual(selected, selected_request)

    def test_contextual_schema_rejects_unknown_ids_and_invalid_lineage(self) -> None:
        schema = planning_schema(
            self.manifest,
            behavior_ids=("behavior-a", "behavior-b"),
            ledger_ids=("entry-000001",),
        )
        value = {
            "schema_version": 2,
            "directions": [
                direction("behavior-a", request("behavior-a", "Use an ensemble.")),
                direction("behavior-b", None),
            ],
            "selection_rationale": "Select the viable direction.",
            "selection": "behavior-a",
        }
        validator = Draft202012Validator(schema)
        validator.validate(value)

        value["selection"] = "behavior-c"
        self.assertTrue(list(validator.iter_errors(value)))
        value["selection"] = "behavior-a"
        value["directions"][0]["request"]["lineage"] = {
            "kind": "new",
            "prior_entry_id": "entry-000001",
        }
        self.assertTrue(list(validator.iter_errors(value)))

    def test_contextual_strategy_schema_rejects_unknown_sources(self) -> None:
        schema = strategy_schema(source_ids=("environment-core",))
        source = schema["properties"]["environment_observations"]["items"]
        source = source["properties"]["evidence"]["items"]["properties"]["source_id"]
        self.assertEqual(source["enum"], ["environment-core"])

    def test_request_must_belong_to_its_direction(self) -> None:
        value = {
            "schema_version": 2,
            "directions": [
                direction("behavior-a", request("behavior-b", "Use an ensemble.")),
                direction("behavior-b", None),
            ],
            "selection_rationale": "Select the only candidate.",
            "selection": "behavior-a",
        }
        with self.assertRaisesRegex(ValidationError, "different direction"):
            validate_planning(
                value,
                strategy=self.strategy,
                ledger=[],
                manifest=self.manifest,
            )

    def test_selection_cannot_reference_an_exhausted_direction(self) -> None:
        value = {
            "schema_version": 2,
            "directions": [
                direction("behavior-a", request("behavior-a", "Use an ensemble.")),
                direction("behavior-b", None),
            ],
            "selection_rationale": "Invalid exhausted selection.",
            "selection": "behavior-b",
        }
        with self.assertRaisesRegex(ValidationError, "selected an exhausted"):
            validate_planning(
                value,
                strategy=self.strategy,
                ledger=[],
                manifest=self.manifest,
            )

    def test_selection_cannot_reference_an_unknown_direction(self) -> None:
        value = {
            "schema_version": 2,
            "directions": [
                direction("behavior-a", request("behavior-a", "Use an ensemble.")),
                direction("behavior-b", None),
            ],
            "selection_rationale": "Invalid unknown selection.",
            "selection": "behavior-c",
        }
        with self.assertRaisesRegex(ValidationError, "selected an unknown"):
            validate_planning(
                value,
                strategy=self.strategy,
                ledger=[],
                manifest=self.manifest,
            )

    def test_candidate_disposition_requires_a_request(self) -> None:
        inconsistent = direction("behavior-a", None)
        inconsistent["disposition"] = "candidate"
        value = {
            "schema_version": 2,
            "directions": [inconsistent, direction("behavior-b", None)],
            "selection_rationale": "Invalid candidate direction.",
            "selection": None,
        }
        with self.assertRaisesRegex(ValidationError, "disposition and request"):
            validate_planning(
                value,
                strategy=self.strategy,
                ledger=[],
                manifest=self.manifest,
            )

    def test_null_selection_requires_every_direction_to_be_exhausted(self) -> None:
        value = {
            "schema_version": 2,
            "directions": [
                direction("behavior-a", request("behavior-a", "Use an ensemble.")),
                direction("behavior-b", None),
            ],
            "selection_rationale": "Incorrectly omit a viable candidate.",
            "selection": None,
        }
        with self.assertRaisesRegex(ValidationError, "only when all directions"):
            validate_planning(
                value,
                strategy=self.strategy,
                ledger=[],
                manifest=self.manifest,
            )


class ExplorationCatalogTests(unittest.TestCase):
    def test_catalog_is_compact_but_canonical_entry_remains_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            large_detail = "diagnostic detail " * 2_000
            saved = add_ledger_entry(
                task,
                {
                    "source": "search:000001",
                    "kind": "research_miss",
                    "rejection_code": "duplicate_hypothesis",
                    "message": "Already tested.",
                    "planning": {"large_private_detail": large_detail},
                },
            )

            catalog = json.loads(
                (task / "exploration" / "ledger.public.jsonl").read_text()
            )
            canonical = json.loads(
                (
                    task
                    / "exploration"
                    / "entries"
                    / f"{saved['entry_id']}.public.json"
                ).read_text()
            )
            self.assertNotIn("planning", catalog)
            self.assertEqual(canonical["planning"]["large_private_detail"], large_detail)
            self.assertLess(len(json.dumps(catalog)), 1_000)

            rebuild_catalog(task)
            self.assertEqual(
                json.loads(
                    (task / "exploration" / "ledger.public.jsonl").read_text()
                ),
                catalog,
            )
