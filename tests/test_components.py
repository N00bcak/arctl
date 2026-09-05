from __future__ import annotations

import unittest
from unittest import mock

from arctl.components import invoke_component, resolve_component
from arctl.errors import ValidationError


class ComponentRegistryTests(unittest.TestCase):
    def test_resolves_installed_component(self) -> None:
        spec = resolve_component("plan", "plan.comparative")
        self.assertTrue(spec.agent_driven)

    def test_rejects_unknown_and_cross_stage_components(self) -> None:
        with self.assertRaisesRegex(ValidationError, "not installed"):
            resolve_component("plan", "plan.missing")
        with self.assertRaisesRegex(ValidationError, "incompatible"):
            resolve_component("plan", "execute.worktree")

    def test_dispatches_through_the_registered_handler(self) -> None:
        handler = mock.Mock(return_value="dispatched")
        module = mock.Mock(_candidate_search=handler)
        with mock.patch(
            "arctl.components.importlib.import_module", return_value=module
        ) as imported:
            result = invoke_component(
                "search", "search.serial-champion", "task", limit=3
            )
        imported.assert_called_once_with("arctl.runner")
        handler.assert_called_once_with("task", limit=3)
        self.assertEqual(result, "dispatched")
