from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from arctl.commands import render_command
from arctl.errors import ValidationError
from arctl.manifest import EvaluatorManifest


def valid_manifest(*, version: int = 3, telemetry: bool = False) -> dict:
    manifest = {
        "schema_version": version,
        "subject_command": ["python3", "subject.py", "{input}", "{output}"],
        "prepare_command": ["python3", "evaluator.py", "{request}", "{response}"],
        "calibrate_command": ["python3", "evaluator.py", "{request}", "{response}"],
        "score_command": ["python3", "evaluator.py", "{request}", "{response}"],
        "limits": {"timeout_seconds": 60, "max_output_bytes": 1000000},
        "schemas": {
            "public_case": {
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "integer"}},
                "additionalProperties": False,
            },
            "subject_result": {
                "type": "object",
                "required": ["score"],
                "properties": {"score": {"type": "number"}},
                "additionalProperties": False,
            },
        },
        "public": {
            "statistic": "expected score",
            "subject_interface": "JSON batch to JSON results",
            "telemetry": (
                {
                    "errors": {
                        "description": "Mean failed actions per case.",
                        "unit": "actions per case",
                        "scope": "paired",
                        "role": "safety",
                        "value_type": "number",
                        "direction": "lower",
                    }
                }
                if version >= 3 and telemetry
                else {}
                if version >= 3
                else []
            ),
        },
        "trial": {
            "meaning": "one generated map",
            "dependence": "trials are independent",
            "seed_to_case": "seed initializes the map generator",
            "subject_visible_seed": False,
        },
        "statistics": {
            "score": "paired mean difference",
            "uncertainty": "one-sided paired bootstrap lower bound",
            "positive_effect": "candidate completes more maps",
        },
        "variation": {
            "known": "runtime scheduling noise",
            "mitigations": ["fixed process affinity"],
        },
        "suspect_test": {
            "trigger": "unusual timeout reduction",
            "reason_codes": ["timeout_shift"],
        },
        "calibration": {},
    }
    manifest["calibration"] = (
        {
            "supported": True,
            "policy": "smallest stable ladder count meeting target precision",
            "ladder": [4, 16, 64, 256],
            "diagnostic": {
                "name": "baseline standard error",
                "units": "score",
                "maximum": 1.0,
            },
        }
        if version >= 2
        else {
            "supported": True,
            "policy": "smallest ladder count meeting target precision",
            "ceiling": 256,
        }
    )
    if version >= 4:
        manifest["setup_contract"] = {
            "environment_adapter": {
                "entrypoint": "demo:Environment",
                "interface": "Python callable",
            },
            "outcome": {
                "direction": "higher",
                "unit": "score",
                "aggregation": "paired mean",
                "extraction": "subject result score",
            },
            "trial": {
                "termination": "map completion",
                "horizon_unit": "actions",
            },
            "hard_rules": ["Keep environment fixed."],
            "runtime_limits": ["60 seconds per process"],
        }
    return manifest


class ManifestTests(unittest.TestCase):
    def test_accepts_complete_manifest_and_trial_modes(self) -> None:
        manifest = EvaluatorManifest.from_mapping(valid_manifest())
        manifest.validate_trial_setting("auto")
        manifest.validate_trial_setting(1)
        manifest.validate_trial_setting(256)
        self.assertEqual(manifest.suspect_reason_codes, ("timeout_shift",))

    def test_manifest_v4_requires_and_parses_the_setup_contract(self) -> None:
        raw = valid_manifest(version=4)
        manifest = EvaluatorManifest.from_mapping(raw)
        self.assertEqual(
            manifest.setup_contract.environment_adapter_entrypoint,
            "demo:Environment",
        )
        raw.pop("setup_contract")
        with self.assertRaisesRegex(ValidationError, "missing=.*setup_contract"):
            EvaluatorManifest.from_mapping(raw)

    def test_semantic_telemetry_contract(self) -> None:
        manifest = EvaluatorManifest.from_mapping(valid_manifest(telemetry=True))
        metric = manifest.public_telemetry["errors"]
        self.assertEqual((metric.scope, metric.role, metric.direction), ("paired", "safety", "lower"))

        raw = valid_manifest(telemetry=True)
        raw["public"]["telemetry"]["errors"]["value_type"] = "boolean"
        with self.assertRaisesRegex(ValidationError, "paired metrics"):
            EvaluatorManifest.from_mapping(raw)

    def test_rejects_partial_unknown_and_duplicate_placeholders(self) -> None:
        for command in (
            ["python3", "subject.py", "--input={input}", "{output}"],
            ["python3", "subject.py", "{input}", "{else}"],
            ["python3", "subject.py", "{input}", "{output}", "{output}"],
        ):
            with self.subTest(command=command):
                raw = valid_manifest()
                raw["subject_command"] = command
                with self.assertRaises(ValidationError):
                    EvaluatorManifest.from_mapping(raw)

    def test_rejects_shells_and_shell_strings(self) -> None:
        for command in (
            "python3 evaluator.py {request} {response}",
            ["bash", "-c", "{request}", "{response}"],
        ):
            with self.subTest(command=command):
                raw = valid_manifest()
                raw["score_command"] = command
                with self.assertRaises(ValidationError):
                    EvaluatorManifest.from_mapping(raw)

    def test_rejects_invalid_json_schemas(self) -> None:
        raw = valid_manifest()
        raw["schemas"]["subject_result"] = {"type": "not-a-json-schema-type"}
        with self.assertRaisesRegex(ValidationError, "not a valid JSON Schema"):
            EvaluatorManifest.from_mapping(raw)

        external = valid_manifest()
        external["schemas"]["public_case"] = {
            "$ref": "https://example.invalid/private-schema.json"
        }
        with self.assertRaisesRegex(ValidationError, "external"):
            EvaluatorManifest.from_mapping(external)

    def test_calibration_contract_is_coherent(self) -> None:
        raw = valid_manifest()
        raw["calibration"] = {
            "supported": False,
            "policy": None,
            "ladder": None,
            "diagnostic": None,
        }
        raw["calibrate_command"] = None
        manifest = EvaluatorManifest.from_mapping(raw)
        with self.assertRaisesRegex(ValidationError, "no calibration"):
            manifest.validate_trial_setting("auto")

        inconsistent = copy.deepcopy(raw)
        inconsistent["calibrate_command"] = [
            "python3",
            "evaluator.py",
            "{request}",
            "{response}",
        ]
        with self.assertRaisesRegex(ValidationError, "must be null"):
            EvaluatorManifest.from_mapping(inconsistent)

    def test_fixed_count_cannot_exceed_approved_ceiling(self) -> None:
        manifest = EvaluatorManifest.from_mapping(valid_manifest())
        with self.assertRaisesRegex(ValidationError, "ceiling"):
            manifest.validate_trial_setting(257)

    def test_controller_pilot_requires_ordered_ladder_and_finite_diagnostic(
        self,
    ) -> None:
        for ladder in ([], [16, 8], [8, 8], [0, 8]):
            with self.subTest(ladder=ladder):
                raw = valid_manifest()
                raw["calibration"]["ladder"] = ladder
                with self.assertRaisesRegex(ValidationError, "ladder"):
                    EvaluatorManifest.from_mapping(raw)

        raw = valid_manifest()
        raw["calibration"]["diagnostic"]["maximum"] = float("inf")
        with self.assertRaisesRegex(ValidationError, "maximum"):
            EvaluatorManifest.from_mapping(raw)

    def test_suspect_trigger_and_reasons_are_coupled(self) -> None:
        raw = valid_manifest()
        raw["suspect_test"] = {"trigger": None, "reason_codes": ["timeout_shift"]}
        with self.assertRaisesRegex(ValidationError, "require a trigger"):
            EvaluatorManifest.from_mapping(raw)

    def test_rendering_allows_only_controller_paths_inside_roots(self) -> None:
        manifest = EvaluatorManifest.from_mapping(valid_manifest())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            request = root / "request.json"
            response = root / "response.json"
            rendered = render_command(
                manifest.score_command,
                {"request": request, "response": response},
                allowed_roots=(root,),
            )
            self.assertEqual(rendered[-2:], (str(request), str(response)))
            with self.assertRaisesRegex(ValidationError, "escapes"):
                render_command(
                    manifest.score_command,
                    {"request": request, "response": root / ".." / "escape.json"},
                    allowed_roots=(root,),
                )
