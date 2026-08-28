from __future__ import annotations

import os
import platform
import unittest
from unittest import mock

from arctl.errors import ProcessError, StateError
from arctl.platform_process import (
    ProcessIdentity,
    _ProcBSDInfo,
    _decode_darwin_process,
    _parse_linux_stat,
    inspect_process,
    process_backend,
)


class PlatformProcessTests(unittest.TestCase):
    def test_parses_linux_identity_with_spaces_and_parentheses_in_name(self) -> None:
        fields = ["S", "1", "42", *(["0"] * 16), "123456"]
        stat = "42 (worker (one)) " + " ".join(fields)

        identity = _parse_linux_stat(stat, expected_pid=42)

        self.assertEqual(
            identity,
            ProcessIdentity(
                platform="Linux",
                pid=42,
                pgid=42,
                start_time=123456,
                state="running",
            ),
        )

    def test_linux_zombie_is_normalized(self) -> None:
        fields = ["Z", "1", "7", *(["0"] * 16), "99"]
        stat = "7 (worker) " + " ".join(fields)

        self.assertEqual(_parse_linux_stat(stat, expected_pid=7).state, "zombie")

    def test_rejects_malformed_linux_identity(self) -> None:
        with self.assertRaisesRegex(ProcessError, "identify managed process"):
            _parse_linux_stat("not a stat record", expected_pid=3)

    def test_decodes_darwin_identity(self) -> None:
        info = _ProcBSDInfo()
        info.pbi_pid = 11
        info.pbi_pgid = 10
        info.pbi_status = 2
        info.pbi_start_tvsec = 123
        info.pbi_start_tvusec = 456

        self.assertEqual(
            _decode_darwin_process(info, expected_pid=11),
            ProcessIdentity(
                platform="Darwin",
                pid=11,
                pgid=10,
                start_time=123_000_456,
                state="running",
            ),
        )

    def test_decodes_darwin_zombie(self) -> None:
        info = _ProcBSDInfo()
        info.pbi_pid = 11
        info.pbi_pgid = 10
        info.pbi_status = 5
        info.pbi_start_tvsec = 123

        self.assertEqual(_decode_darwin_process(info, expected_pid=11).state, "zombie")

    def test_rejects_unsupported_platform(self) -> None:
        with self.assertRaisesRegex(StateError, "Linux and macOS"):
            process_backend("Windows")

    def test_inspects_current_process_on_supported_host(self) -> None:
        if platform.system() not in {"Linux", "Darwin"}:
            self.skipTest("requires a supported arctl host")

        identity = inspect_process(os.getpid())

        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(identity.pid, os.getpid())
        self.assertGreater(identity.start_time, 0)
        self.assertGreater(identity.pgid, 0)
        self.assertEqual(identity.platform, platform.system())

    def test_dispatches_without_probing_an_unsupported_backend(self) -> None:
        with (
            mock.patch("arctl.platform_process.platform.system", return_value="Plan9"),
            self.assertRaisesRegex(StateError, "Linux and macOS"),
        ):
            inspect_process(os.getpid())


if __name__ == "__main__":
    unittest.main()
