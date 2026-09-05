from __future__ import annotations

import math
import unittest

from arctl.errors import StateError, ValidationError
from arctl.models import Evidence, ResearchRequest, TaskConfig
from arctl.search import validate_research_links, validate_strategy_links
from arctl.manifest import TelemetryMetric

from .helpers import valid_evidence, valid_task


class TaskConfigTests(unittest.TestCase):
    def test_declares_public_probe_trial_equivalents(self) -> None:
        task = TaskConfig.from_mapping(valid_task())
        self.assertEqual(task.public_probe, ("python3", "tools/probe.py"))
        self.assertEqual(task.public_probe_trial_equivalents, 3)

        raw = valid_task()
        raw["public_probe"]["trial_equivalents"] = 0
        with self.assertRaisesRegex(ValidationError, "trial_equivalents"):
            TaskConfig.from_mapping(raw)

    def test_resolves_serial_and_hotseat_method_profiles(self) -> None:
        serial = TaskConfig.from_mapping(valid_task())
        assert serial.method is not None
        self.assertEqual(serial.method.profile, "serial")
        self.assertEqual(len(serial.method.pool("strategize")), 1)
        self.assertEqual(serial.strategy_model, "gpt-5.6-sol")
        self.assertEqual(serial.environment_sources[0].commit, "b" * 40)

        raw = valid_task(hotseat=True)
        raw["method"]["agents"] = {
            "critic": {
                "backend": "codex-cli",
                "model": "critic-model",
                "settings": {"reasoning_effort": "high"},
            }
        }
        raw["method"]["overrides"] = {
            "strategize": {
                "component": "strategize.environment",
                "agent_pool": ["strategy-a", "critic"],
            }
        }
        hotseat = TaskConfig.from_mapping(raw)
        assert hotseat.method is not None
        self.assertEqual(hotseat.method.profile, "serial-hotseat")
        self.assertEqual(hotseat.method.pool("strategize")[1].model, "critic-model")

    def test_rejects_cross_component_and_unknown_agent_assignments(self) -> None:
        raw = valid_task(hotseat=True)
        raw["method"]["overrides"] = {
            "strategize": {
                "component": "execute.worktree",
                "agent_pool": [],
            }
        }
        with self.assertRaisesRegex(ValidationError, "incompatible"):
            TaskConfig.from_mapping(raw)

        raw = valid_task(hotseat=True)
        raw["method"]["overrides"] = {
            "reflect": {
                "component": "reflect.evidence",
                "agent_pool": ["reflection-a", "missing"],
            }
        }
        with self.assertRaisesRegex(ValidationError, "unknown agent"):
            TaskConfig.from_mapping(raw)

    def test_backend_identifiers_are_resolved_at_approval_not_parse_time(self) -> None:
        raw = valid_task()
        raw["method"]["agents"] = {
            "experimental": {
                "backend": "fake",
                "model": "adapter-under-test",
                "settings": {"reasoning_effort": "low"},
            }
        }
        raw["method"]["overrides"] = {
            "plan": {
                "component": "plan.comparative",
                "agent_pool": ["experimental"],
            }
        }
        task = TaskConfig.from_mapping(raw)
        assert task.method is not None
        self.assertEqual(task.method.pool("plan")[0].backend, "fake")
        self.assertNotIn("certification", task.method.to_lock()["agents"]["experimental"])

    def test_accepts_strict_task(self) -> None:
        task = TaskConfig.from_mapping(valid_task())
        self.assertEqual(task.task_id, "demo")
        self.assertEqual(task.trials, "auto")
        self.assertEqual(task.public_checks[0][0], "python3")

    def test_rejects_unknown_fields(self) -> None:
        raw = valid_task()
        raw["metric"] = "accuracy"
        with self.assertRaisesRegex(ValidationError, "extra=.*metric"):
            TaskConfig.from_mapping(raw)

    def test_agent_model_strict_override(self) -> None:
        raw = valid_task()
        raw["method"]["agents"] = {
            "strategy-custom": {
                "backend": "codex-cli",
                "model": "custom-model",
                "settings": {"reasoning_effort": "xhigh"},
            },
            "execution-fast": {
                "backend": "codex-cli",
                "model": "fast-model",
                "settings": {"reasoning_effort": "low"},
            },
        }
        raw["method"]["overrides"] = {
            "strategize": {
                "component": "strategize.environment",
                "agent_pool": ["strategy-custom"],
            },
            "execute": {
                "component": "execute.worktree",
                "agent_pool": ["execution-fast"],
            },
        }
        configured = TaskConfig.from_mapping(raw)
        self.assertEqual(configured.strategy_model, "custom-model")
        self.assertEqual(configured.strategy_reasoning_effort, "xhigh")
        self.assertEqual(configured.execution_model, "fast-model")
        self.assertEqual(configured.execution_reasoning_effort, "low")

        raw["method"]["agents"]["strategy-custom"]["settings"]["reasoning_effort"] = "extreme"
        with self.assertRaisesRegex(ValidationError, "reasoning_effort"):
            TaskConfig.from_mapping(raw)

        raw["method"]["agents"]["strategy-custom"]["settings"]["reasoning_effort"] = "high"
        raw["method"]["agents"]["execution-fast"]["settings"]["reasoning_effort"] = "extreme"
        with self.assertRaisesRegex(ValidationError, "reasoning_effort"):
            TaskConfig.from_mapping(raw)

    def test_planning_reflection_roles_and_unlimited_budget(self) -> None:
        raw = valid_task()
        raw["method"]["agents"] = {
            "planner": {"backend": "codex-cli", "model": "planner", "settings": {"reasoning_effort": "medium"}},
            "reflector": {"backend": "codex-cli", "model": "reflector", "settings": {"reasoning_effort": "medium"}},
        }
        raw["method"]["overrides"] = {
            "plan": {"component": "plan.comparative", "agent_pool": ["planner"]},
            "reflect": {"component": "reflect.evidence", "agent_pool": ["reflector"]},
        }
        raw["max_experiments"] = "unlimited"

        configured = TaskConfig.from_mapping(raw)

        self.assertEqual(configured.planning_model, "planner")
        self.assertEqual(configured.planning_reasoning_effort, "medium")
        self.assertEqual(configured.reflection_model, "reflector")
        self.assertEqual(configured.reflection_reasoning_effort, "medium")
        self.assertIsNone(configured.max_experiments)

    def test_optional_candidate_review_is_strict_and_bounded(self) -> None:
        raw = valid_task()
        raw["candidate_review"] = {
            "contract": "Use only declared observations.",
            "checks": [["python3", "tools/policy_guard.py"]],
            "repair_attempts": 1,
        }
        configured = TaskConfig.from_mapping(raw)
        assert configured.candidate_review is not None
        self.assertEqual(configured.candidate_review.repair_attempts, 1)
        self.assertEqual(configured.candidate_review.checks[0][1], "tools/policy_guard.py")

        raw["candidate_review"]["repair_attempts"] = 2
        with self.assertRaisesRegex(ValidationError, "must equal 0 or 1"):
            TaskConfig.from_mapping(raw)

    def test_requires_environment_and_agent_settings(self) -> None:
        raw = valid_task()
        del raw["method"]
        with self.assertRaisesRegex(ValidationError, "fields differ"):
            TaskConfig.from_mapping(raw)

        raw = valid_task()
        del raw["environment"]
        with self.assertRaisesRegex(ValidationError, "fields differ"):
            TaskConfig.from_mapping(raw)

    def test_rejects_shell_command_strings(self) -> None:
        raw = valid_task()
        raw["public_probe"] = "python3 tools/probe.py"
        with self.assertRaisesRegex(ValidationError, "object"):
            TaskConfig.from_mapping(raw)

    def test_rejects_boolean_trial_count(self) -> None:
        raw = valid_task()
        raw["trials"] = True
        with self.assertRaisesRegex(ValidationError, "trials"):
            TaskConfig.from_mapping(raw)

    def test_rejects_relative_repositories(self) -> None:
        raw = valid_task()
        raw["repo"] = "subject"
        with self.assertRaisesRegex(ValidationError, "absolute"):
            TaskConfig.from_mapping(raw)

    def test_environment_sources_are_typed_and_linked(self) -> None:
        task = TaskConfig.from_mapping(valid_task())
        self.assertEqual(task.environment_sources[0].kind, "implementation")
        self.assertEqual(task.environment_probes[0][0], "python3")

        raw = valid_task()
        raw["environment"]["probes"][0]["backed_by"] = ["missing"]
        with self.assertRaisesRegex(ValidationError, "unknown codebase"):
            TaskConfig.from_mapping(raw)

        raw = valid_task()
        raw["environment"]["codebases"][0]["id"] = "environment-probe"
        raw["environment"]["probes"][0]["backed_by"] = ["environment-probe"]
        with self.assertRaisesRegex(ValidationError, "duplicate environment source"):
            TaskConfig.from_mapping(raw)

    def test_rejects_task_id_path_and_ref_escapes(self) -> None:
        for task_id in ("../escape", "bad/name", ".hidden", "space name"):
            with self.subTest(task_id=task_id):
                raw = valid_task()
                raw["task_id"] = task_id
                with self.assertRaisesRegex(ValidationError, "task_id"):
                    TaskConfig.from_mapping(raw)


class EvidenceTests(unittest.TestCase):
    def parse(self, raw: dict, kind: str = "primary") -> Evidence:
        return Evidence.from_mapping(
            raw,
            expected_kind=kind,  # type: ignore[arg-type]
            expected_trial_count=128,
            allowed_suspect_reasons=("distribution_shift",),
        )

    def test_accepts_exact_valid_schema(self) -> None:
        evidence = self.parse(valid_evidence())
        self.assertEqual(evidence.effect_estimate, 0.037)

    def test_rejects_nonfinite_numbers(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValidationError, "finite"):
                    self.parse(valid_evidence(estimate=value))

    def test_rejects_lower_bound_above_estimate(self) -> None:
        with self.assertRaisesRegex(ValidationError, "must not exceed"):
            self.parse(valid_evidence(estimate=0.1, lower=0.2))

    def test_rejects_identity_and_trial_mismatches(self) -> None:
        raw = valid_evidence(kind="suspect")
        with self.assertRaisesRegex(ValidationError, "kind"):
            self.parse(raw)
        raw = valid_evidence()
        raw["trial_count"] = 127
        with self.assertRaisesRegex(ValidationError, "trial_count"):
            self.parse(raw)

    def test_rejects_unapproved_telemetry(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unapproved telemetry"):
            self.parse(valid_evidence(telemetry={"secret_metric": 1}))

    def test_primary_can_request_one_approved_suspect_test(self) -> None:
        evidence = self.parse(
            valid_evidence(
                suspect_required=True,
                suspect_reason="distribution_shift",
            )
        )
        self.assertTrue(evidence.suspect_required)

    def test_suspect_request_requires_an_otherwise_accepted_result(self) -> None:
        for changes in (
            {"lower": 0.0},
            {"estimate": 0.0, "lower": 0.0},
            {"hard_rules_pass": False},
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValidationError, "otherwise accepted"):
                    self.parse(
                        valid_evidence(
                            suspect_required=True,
                            suspect_reason="distribution_shift",
                            **changes,
                        )
                    )

    def test_suspect_cannot_request_recursion_or_emit_telemetry(self) -> None:
        raw = valid_evidence(
            kind="suspect",
            suspect_required=True,
            suspect_reason="distribution_shift",
        )
        with self.assertRaisesRegex(ValidationError, "cannot request"):
            self.parse(raw, "suspect")

    def test_rejects_reason_without_request(self) -> None:
        with self.assertRaisesRegex(ValidationError, "reason must be null"):
            self.parse(valid_evidence(suspect_reason="distribution_shift"))

    def test_public_telemetry_is_semantic_complete_and_finite(self) -> None:
        metrics = {
            "count": TelemetryMetric(
                "Mean count", "items", "paired", "outcome", "number", "higher"
            ),
            "passed": TelemetryMetric(
                "Constraint result", "boolean", "comparison", "safety", "boolean", "contextual"
            ),
        }
        evidence = Evidence.from_mapping(
            valid_evidence(
                telemetry={
                    "count": {"champion": 1.0, "candidate": 2.0},
                    "passed": {"value": True},
                }
            ),
            expected_kind="primary",
            expected_trial_count=128,
            allowed_telemetry=metrics,
        )
        self.assertEqual(evidence.telemetry["count"]["candidate"], 2.0)
        for invalid in (math.nan, math.inf, "text", [], {}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    Evidence.from_mapping(
                        valid_evidence(
                            telemetry={
                                "count": {"champion": 1, "candidate": invalid},
                                "passed": {"value": True},
                            }
                        ),
                        expected_kind="primary",
                        expected_trial_count=128,
                        allowed_telemetry=metrics,
                    )

        with self.assertRaisesRegex(ValidationError, "missing"):
            Evidence.from_mapping(
                valid_evidence(telemetry={"count": {"champion": 1, "candidate": 2}}),
                expected_kind="primary",
                expected_trial_count=128,
                allowed_telemetry=metrics,
            )


class ResearchRequestTests(unittest.TestCase):
    def valid(self) -> dict:
        return {
            "strategy_behavior_id": "preserve-options",
            "claim": "Prefer recoverable routes.",
            "mechanism": "Penalize branches without retreat.",
            "viability": "The champion exposes a branch score that can represent retreat.",
            "evidence_review": {
                "summary": "No prior experiment bears directly on this mechanism.",
                "citations": [],
            },
            "expected_effect": "Complete more maps.",
            "expected_telemetry": {"dead_ends": "decrease"},
            "falsifiers": ["The paired effect is not positive."],
            "lineage": {"kind": "new", "prior_entry_id": None},
        }

    def test_accepts_hypothesis_with_allowlisted_telemetry(self) -> None:
        request = ResearchRequest.from_mapping(
            self.valid(),
            allowed_telemetry=("dead_ends",),
        )
        self.assertEqual(request.claim, "Prefer recoverable routes.")

    def test_rejects_unapproved_telemetry_and_empty_falsifiers(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unapproved telemetry"):
            ResearchRequest.from_mapping(self.valid(), allowed_telemetry=())
        raw = self.valid()
        raw["falsifiers"] = []
        with self.assertRaisesRegex(ValidationError, "must not be empty"):
            ResearchRequest.from_mapping(raw, allowed_telemetry=("dead_ends",))

    def test_strategy_and_ledger_links_are_validated(self) -> None:
        strategy = {
            "successful_policy_behaviors": [{"id": "preserve-options"}],
        }
        ledger = [{"entry_id": "entry-000001"}]
        request = self.valid()
        request["strategy_behavior_id"] = "preserve-options"
        request["evidence_review"]["citations"] = [
            {
                "entry_id": "entry-000001",
                "bearing": "supports",
                "finding": "The earlier result supports this mechanism.",
            }
        ]
        validate_research_links(request, strategy=strategy, ledger=ledger)

        request["strategy_behavior_id"] = "unknown"
        with self.assertRaisesRegex(ValidationError, "unknown strategy behavior"):
            validate_research_links(request, strategy=strategy, ledger=ledger)
        request["strategy_behavior_id"] = "preserve-options"
        request["evidence_review"]["citations"][0]["entry_id"] = "entry-999999"
        with self.assertRaisesRegex(ValidationError, "unknown ledger entry"):
            validate_research_links(request, strategy=strategy, ledger=ledger)

        request["evidence_review"]["citations"][0]["entry_id"] = "entry-000001"
        ledger[0]["scientific_status"] = "untested"
        with self.assertRaisesRegex(ValidationError, "cannot support"):
            validate_research_links(request, strategy=strategy, ledger=ledger)
        request["evidence_review"]["citations"][0]["bearing"] = "unresolved"
        validate_research_links(request, strategy=strategy, ledger=ledger)

        ledger[0]["scientific_status"] = "inconclusive"
        request["evidence_review"]["citations"][0]["bearing"] = "supports"
        with self.assertRaisesRegex(ValidationError, "inconclusive experiments"):
            validate_research_links(request, strategy=strategy, ledger=ledger)

    def test_refinement_must_cite_its_prior_entry(self) -> None:
        strategy = {"successful_policy_behaviors": [{"id": "preserve-options"}]}
        ledger = [
            {"entry_id": "entry-000001", "scientific_status": "inconclusive"}
        ]
        request = self.valid()
        request["strategy_behavior_id"] = "preserve-options"
        request["lineage"] = {
            "kind": "refinement",
            "prior_entry_id": "entry-000001",
        }

        with self.assertRaisesRegex(ValidationError, "must cite its prior"):
            validate_research_links(request, strategy=strategy, ledger=ledger)


class StrategyContractTests(unittest.TestCase):
    def test_observations_and_behaviors_must_cite_known_ids(self) -> None:
        strategy = {
            "environment_observations": [
                {
                    "id": "board-geometry",
                    "evidence": [{"source_id": "environment-core"}],
                }
            ],
            "environment_uncertainties": [],
            "successful_policy_behaviors": [
                {"id": "preserve-space", "derived_from": ["board-geometry"]}
            ],
        }
        validate_strategy_links(strategy, source_ids={"environment-core"})
        strategy["environment_observations"][0]["evidence"][0]["source_id"] = "policy"
        with self.assertRaisesRegex(StateError, "unknown environment source"):
            validate_strategy_links(strategy, source_ids={"environment-core"})

    def test_behaviors_may_be_grounded_in_declared_uncertainties(self) -> None:
        strategy = {
            "environment_observations": [
                {
                    "id": "visible-preview",
                    "evidence": [{"source_id": "environment-core"}],
                }
            ],
            "environment_uncertainties": [{"id": "unseen-continuation"}],
            "successful_policy_behaviors": [
                {
                    "id": "robust-planning",
                    "derived_from": ["visible-preview", "unseen-continuation"],
                }
            ],
        }
        validate_strategy_links(strategy, source_ids={"environment-core"})

        strategy["successful_policy_behaviors"][0]["derived_from"] = ["missing"]
        with self.assertRaisesRegex(StateError, "unknown environment grounding"):
            validate_strategy_links(strategy, source_ids={"environment-core"})
