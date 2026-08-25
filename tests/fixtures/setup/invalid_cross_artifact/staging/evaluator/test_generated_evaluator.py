"""Deliberately demonstrates why missing imports must not become skips."""

import unittest

try:
    import missing_runtime_dependency
except ImportError:
    missing_runtime_dependency = None


class GeneratedEvaluatorTests(unittest.TestCase):
    @unittest.skipIf(
        missing_runtime_dependency is None,
        "missing runtime dependency",
    )
    def test_runtime_dependency_is_available(self):
        self.assertIsNotNone(missing_runtime_dependency)
