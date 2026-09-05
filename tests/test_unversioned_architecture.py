from __future__ import annotations

import re
import unittest
from pathlib import Path

from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from arctl.errors import ValidationError
from arctl.manifest import EvaluatorManifest
from arctl.models import TaskConfig
from arctl.runner import _validate_implementation_report

from .helpers import valid_task
from .test_manifest import valid_manifest


ROOT = Path(__file__).resolve().parents[1]


class UnversionedArchitectureTests(unittest.TestCase):
    def test_owned_sources_docs_and_fixtures_have_no_format_generations(self) -> None:
        forbidden_literals = (
            "schema" + "_version",
            "_up" + "grade_",
            "serial-" + "v" + "1",
            "serial-hotseat-" + "v" + "1",
            "codex-cli-" + "v" + "1",
            "conversation-" + "v" + "2",
            "arctl-agent-conformance-" + "v" + "1",
        )
        numbered_contract = re.compile(
            r"\b(?:schema|task|manifest|report|contract|record|format)[ -]v[0-9]+\b",
            re.IGNORECASE,
        )
        roots = (ROOT / "src", ROOT / "docs", ROOT / "tests" / "fixtures")
        suffixes = {".py", ".md", ".json", ".yaml", ".yml"}
        violations: list[str] = []
        for root in roots:
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix not in suffixes:
                    continue
                text = path.read_text(errors="replace")
                for literal in forbidden_literals:
                    if literal in text:
                        violations.append(f"{path.relative_to(ROOT)}: {literal}")
                if match := numbered_contract.search(text):
                    violations.append(
                        f"{path.relative_to(ROOT)}: {match.group(0)}"
                    )
        self.assertEqual(violations, [])

    def test_obsolete_version_selector_is_rejected_by_strict_contracts(self) -> None:
        obsolete_field = "schema" + "_version"

        task = valid_task()
        task[obsolete_field] = 5
        with self.assertRaisesRegex(ValidationError, "task fields differ"):
            TaskConfig.from_mapping(task)

        manifest = valid_manifest()
        manifest[obsolete_field] = 4
        with self.assertRaisesRegex(ValidationError, "manifest fields differ"):
            EvaluatorManifest.from_mapping(manifest)

        implementation = {
            "status": "implemented",
            "summary": "Implemented and verified.",
            "deviations": [],
            "requirements": [
                {
                    "id": "requirement",
                    "requirement": "Implement the frozen mechanism.",
                    "status": "verified",
                    "evidence": "The targeted probe passed.",
                    "verification_ids": ["probe"],
                }
            ],
            "verifications": [
                {
                    "id": "probe",
                    "purpose": "Exercise the mechanism.",
                    "command": "python3 probe.py",
                    "outcome": "passed",
                    "evidence": "The command exited successfully.",
                }
            ],
            obsolete_field: 3,
        }
        with self.assertRaises(JsonSchemaValidationError):
            _validate_implementation_report(implementation)


if __name__ == "__main__":
    unittest.main()
