from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arctl.downstream import RetryPolicy, primary_process_error, transient_process_error
from arctl.errors import TransientDownstreamError


class DownstreamTests(unittest.TestCase):
    def test_terminal_codex_error_wins_over_failed_tool_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            process = Path(temporary)
            events = [
                {"type": "item.completed", "item": {"status": "failed"}},
                {"type": "error", "message": "Selected model is at capacity."},
                {
                    "type": "turn.failed",
                    "error": {"message": "Selected model is at capacity. Please retry."},
                },
            ]
            (process / "stdout.bin").write_text(
                "\n".join(json.dumps(event) for event in events)
            )
            (process / "stderr.bin").write_text("unrelated command failure\n")

            self.assertEqual(
                primary_process_error(process),
                "Selected model is at capacity. Please retry.",
            )
            error = transient_process_error(process, stage="execution", codex=True)
            assert error is not None
            self.assertEqual(error.category, "capacity")
            self.assertEqual(error.artifact_path, str(process.resolve()))

    def test_network_traceback_is_transient_for_public_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            process = Path(temporary)
            (process / "stderr.bin").write_text(
                "Traceback (most recent call last):\n"
                "  File 'probe.py', line 1, in <module>\n"
                "urllib.error.HTTPError: HTTP Error 503: Service Unavailable\n"
            )
            error = transient_process_error(process, stage="public check 1", codex=False)
            assert error is not None
            self.assertEqual(error.category, "network")
            self.assertIn("HTTPError", error.detail)

    def test_retry_budget_is_consecutive_and_resets_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events: list[dict] = []
            policy = RetryPolicy(
                1,
                0,
                progress=events.append,
                stop_path=Path(temporary) / "stop",
            )
            error = TransientDownstreamError("execution", "capacity", "busy", temporary)
            policy.wait(error)
            policy.succeeded()
            policy.wait(error)
            with self.assertRaises(TransientDownstreamError) as raised:
                policy.wait(error)
            self.assertEqual(raised.exception.retries_used, 1)
            self.assertEqual(len(events), 2)


if __name__ == "__main__":
    unittest.main()
