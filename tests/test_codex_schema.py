from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arctl.candidate_review import repair_schema, review_schema as candidate_review_schema
from arctl.codex_schema import validate_codex_output_schema
from arctl.errors import StateError
from arctl.manifest import EvaluatorManifest
from arctl.reflection import reflection_schema
from arctl.runner import _implementation_schema, _research_schema
from arctl.sandbox import research_command
from arctl.search import planning_schema, research_schema, strategy_schema
from arctl.setup import (
    _direct_build_schema,
    build_schema,
    discovery_schema,
    review_schema as setup_review_schema,
)
from arctl.setup_conversation import batch_schema, finalized_design_schema

from .test_manifest import valid_manifest


class CodexSchemaTests(unittest.TestCase):
    def test_every_agent_output_schema_matches_the_codex_subset(self) -> None:
        manifest = EvaluatorManifest.from_mapping(valid_manifest(telemetry=True))
        schemas = {
            "setup discovery": discovery_schema(),
            "setup conversation": batch_schema(
                revision=2,
                decision_ids=("objective", "outcome", "policy_boundary"),
            ),
            "finalized setup design": finalized_design_schema(),
            "setup build": build_schema(),
            "setup review": setup_review_schema(),
            "direct setup build": _direct_build_schema(),
            "strategy": strategy_schema(source_ids=("source-a",)),
            "research with no prior ledger": research_schema(
                manifest,
                behavior_ids=("behavior-a",),
                ledger_ids=(),
            ),
            "research with prior ledger": research_schema(
                manifest,
                behavior_ids=("behavior-a",),
                ledger_ids=("entry-a",),
            ),
            "planning": planning_schema(
                manifest,
                behavior_ids=("behavior-a",),
                ledger_ids=("entry-a",),
            ),
            "reflection": reflection_schema(
                metric_names=tuple(manifest.public_telemetry),
                strategy_behavior_id="behavior-a",
                history_entry_ids=("entry-a",),
            ),
            "candidate review": candidate_review_schema(),
            "candidate repair": repair_schema(),
            "implementation": _implementation_schema(),
            "runner research": _research_schema(manifest),
        }

        for name, schema in schemas.items():
            with self.subTest(name=name):
                validate_codex_output_schema(schema)

    def test_repository_citation_digest_is_required_and_nullable(self) -> None:
        citation = batch_schema()["properties"]["questions"]["items"]["properties"][
            "options"
        ]["items"]["properties"]["citations"]["items"]["anyOf"][0]

        self.assertIn("excerpt_sha256", citation["required"])
        self.assertEqual(
            citation["properties"]["excerpt_sha256"]["type"],
            ["string", "null"],
        )

    def test_setup_batch_binds_the_controller_owned_revision(self) -> None:
        schema = batch_schema(
            revision=7,
            decision_ids=("objective", "outcome", "policy_boundary"),
        )

        self.assertEqual(
            schema["properties"]["revision"],
            {"type": "integer", "const": 7},
        )
        validate_codex_output_schema(schema)

        decision_refs = schema["properties"]["design"]["anyOf"][1]["properties"][
            "objective"
        ]["properties"]["decision_refs"]
        self.assertEqual(
            decision_refs["items"]["enum"],
            ["objective", "outcome", "policy_boundary"],
        )

    def test_setup_batch_with_no_decisions_forbids_decision_references(self) -> None:
        schema = batch_schema(revision=1, decision_ids=())
        decision_refs = schema["properties"]["design"]["anyOf"][1]["properties"][
            "objective"
        ]["properties"]["decision_refs"]

        self.assertEqual(decision_refs["maxItems"], 0)
        validate_codex_output_schema(schema)

    def test_reflection_requires_bounded_metric_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires metric_names"):
            reflection_schema()

    def test_production_reflection_schema_reaches_codex_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema_path = root / "scratch" / "reflection.schema.json"
            schema_path.parent.mkdir()
            schema = reflection_schema(
                metric_names=("errors",),
                strategy_behavior_id="avoid-errors",
                history_entry_ids=("entry-a",),
            )
            schema_path.write_text(json.dumps(schema), encoding="utf-8")

            command = research_command(
                worktree=root / "candidate",
                scratch=schema_path.parent,
                output_schema=schema_path,
                prompt="reflect",
                writable_worktree=False,
            )

            self.assertIn("--output-schema", command)
            self.assertEqual(
                command[command.index("--output-schema") + 1],
                str(schema_path.resolve()),
            )

    def test_unsupported_composition_is_rejected_with_its_path(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "value": {
                    "allOf": [{"type": "string"}],
                }
            },
            "required": ["value"],
            "additionalProperties": False,
        }

        with self.assertRaisesRegex(
            StateError,
            r"unsupported keywords at \$\.properties\.value: \['allOf'\]",
        ):
            validate_codex_output_schema(schema)

    def test_composite_const_is_rejected_locally(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "const": ["arctl_policy.py"],
                }
            },
            "required": ["paths"],
            "additionalProperties": False,
        }

        with self.assertRaisesRegex(StateError, "const must be a scalar"):
            validate_codex_output_schema(schema)


if __name__ == "__main__":
    unittest.main()
