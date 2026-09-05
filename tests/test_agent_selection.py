from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arctl.agent_backend import (
    AgentSessionRequest,
    BACKEND_ADAPTERS,
    agent_command,
    agent_provenance,
    validate_method_backends,
)
from arctl.agent_selection import select_agent
from arctl.errors import ValidationError
from arctl.models import TaskConfig

from .helpers import fake_backend_adapter, valid_task


def fake_hotseat_task() -> TaskConfig:
    raw = valid_task(hotseat=True)
    raw["method"]["allow_unverified_isolation"] = True
    raw["method"]["agents"] = {
        name: {
            "backend": "fake",
            "model": name,
            "settings": {"reasoning_effort": "low"},
        }
        for name in ("alpha", "beta")
    }
    raw["method"]["overrides"] = {
        component: {
            "component": identifier,
            "agent_pool": ["alpha", "beta"],
        }
        for component, identifier in {
            "strategize": "strategize.environment",
            "plan": "plan.comparative",
            "execute": "execute.worktree",
            "reflect": "reflect.evidence",
        }.items()
    }
    return TaskConfig.from_mapping(raw)


class AgentSelectionTests(unittest.TestCase):
    def test_uniform_draw_is_persisted_and_reused_for_recovery(self) -> None:
        task = fake_hotseat_task()
        assert task.method is not None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = select_agent(
                task.method,
                component="strategize",
                lifecycle="strategy:000001",
                root=root,
                chooser=lambda pool: pool[1],
            )
            second = select_agent(
                task.method,
                component="strategize",
                lifecycle="strategy:000001",
                root=root,
                chooser=lambda pool: self.fail("recovery redrew the agent"),
            )
            self.assertEqual(first.name, "beta")
            self.assertEqual(second, first)

    def test_selected_fake_agent_takes_the_stage_session(self) -> None:
        task = fake_hotseat_task()
        assert task.method is not None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = select_agent(
                task.method,
                component="plan",
                lifecycle="planning:000001:01",
                root=root,
                chooser=lambda pool: "beta",
            )
            request = AgentSessionRequest(
                worktree=root,
                scratch=root / "output",
                output_schema=root / "schema.json",
                prompt="plan",
                output_name="planning.public.json",
            )
            with mock.patch.dict(BACKEND_ADAPTERS, {"fake": fake_backend_adapter()}):
                command = agent_command(selected, request)
            self.assertEqual(command, ("fake-agent", "beta", "planning.public.json"))

    def test_execute_substages_draw_independently_with_replacement(self) -> None:
        task = fake_hotseat_task()
        assert task.method is not None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = [
                select_agent(
                    task.method,
                    component="execute",
                    lifecycle=lifecycle,
                    root=root / lifecycle,
                    chooser=lambda pool: pool[0],
                ).name
                for lifecycle in ("implementation", "review", "repair")
            ]
            self.assertEqual(selected, ["alpha", "alpha", "alpha"])

    def test_certification_promotion_is_prospective_not_semantic(self) -> None:
        task = fake_hotseat_task()
        assert task.method is not None
        semantic_lock = task.method.to_lock()
        with mock.patch.dict(
            BACKEND_ADAPTERS,
            {"fake": fake_backend_adapter(certification="experimental")},
        ):
            approved = validate_method_backends(task.method)
            old = agent_provenance(task.method.pool("plan")[0], lifecycle="plan:1")
        with mock.patch.dict(
            BACKEND_ADAPTERS,
            {"fake": fake_backend_adapter(certification="verified")},
        ):
            promoted = validate_method_backends(task.method)
            new = agent_provenance(task.method.pool("plan")[0], lifecycle="plan:2")
        self.assertEqual(task.method.to_lock(), semantic_lock)
        self.assertEqual(approved["fake"]["certification"], "experimental")
        self.assertEqual(promoted["fake"]["certification"], "verified")
        self.assertEqual(old["certification"], "experimental")
        self.assertEqual(new["certification"], "verified")

    def test_unavailable_and_unapproved_experimental_adapters_fail_preflight(self) -> None:
        task = fake_hotseat_task()
        assert task.method is not None
        with self.assertRaisesRegex(ValidationError, "not installed"):
            validate_method_backends(task.method)
        raw = valid_task()
        raw["method"]["agents"] = {
            "experimental": {
                "backend": "fake",
                "model": "fake",
                "settings": {"reasoning_effort": "low"},
            }
        }
        raw["method"]["overrides"] = {
            "plan": {
                "component": "plan.comparative",
                "agent_pool": ["experimental"],
            }
        }
        disallowed = TaskConfig.from_mapping(raw)
        assert disallowed.method is not None
        with mock.patch.dict(BACKEND_ADAPTERS, {"fake": fake_backend_adapter()}):
            with self.assertRaisesRegex(ValidationError, "unverified isolation"):
                validate_method_backends(disallowed.method)
