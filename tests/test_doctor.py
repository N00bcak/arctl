from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arctl.doctor import (
    _process_cleanup_probe,
    _profile_probe,
    doctor_succeeded,
    run_doctor,
)
from arctl.errors import ProcessError
from arctl.platform_process import ProcessIdentity


class DoctorTests(unittest.TestCase):
    def test_cleanup_probe_allows_descendant_state_to_converge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def timed_out(directory, *args, **kwargs):
                root.joinpath("child.pid").write_text("17")
                raise ProcessError("process timed out")

            with (
                mock.patch("arctl.doctor.run_once", side_effect=timed_out),
                mock.patch(
                    "arctl.doctor.inspect_process",
                    side_effect=[
                        ProcessIdentity("Darwin", 17, 16, 1, "running"),
                        None,
                    ],
                ),
                mock.patch("arctl.doctor.time.sleep") as sleep,
            ):
                self.assertTrue(_process_cleanup_probe(root))

        sleep.assert_called_once_with(0.01)

    def test_nested_seatbelt_failure_has_actionable_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowed_read = root / "allowed"
            denied_read = root / "denied"
            allowed_write = root / "output"
            denied_write = root / "private"
            allowed_read.write_text("allowed")
            denied_read.write_text("private")
            allowed_write.mkdir()
            denied_write.mkdir()
            with (
                mock.patch("arctl.doctor.shutil.which", return_value="/usr/bin/codex"),
                mock.patch(
                    "arctl.doctor.subprocess.run",
                    return_value=SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="sandbox-exec: sandbox_apply: Operation not permitted",
                    ),
                ),
            ):
                passed, diagnostic = _profile_probe(
                    root,
                    profile="arctl-subject",
                    cwd=root,
                    allowed_read=allowed_read,
                    denied_read=denied_read,
                    allowed_write=allowed_write,
                    denied_write=denied_write,
                    read_paths=(root,),
                    write_paths=(allowed_write,),
                )

        self.assertFalse(passed)
        assert diagnostic is not None
        self.assertIn("normal unsandboxed Terminal", diagnostic)

    def test_missing_prerequisite_short_circuits_privileged_probes(self) -> None:
        def which(name: str) -> str | None:
            return None if name == "codex" else f"/usr/bin/{name}"

        with (
            mock.patch("arctl.doctor.platform.system", return_value="Linux"),
            mock.patch("arctl.doctor.shutil.which", side_effect=which),
            mock.patch(
                "arctl.doctor.inspect_process",
                return_value=ProcessIdentity("Linux", 1, 1, 1, "running"),
            ),
            mock.patch("arctl.doctor._profile_probe") as profile,
            mock.patch("arctl.doctor._process_cleanup_probe") as cleanup,
        ):
            report = run_doctor()

        self.assertEqual(report["schema_version"], 2)
        self.assertFalse(report["checks"]["codex"])
        self.assertFalse(doctor_succeeded(report))
        profile.assert_not_called()
        cleanup.assert_not_called()

    def test_reports_each_isolation_profile_and_process_cleanup(self) -> None:
        with (
            mock.patch("arctl.doctor.platform.system", return_value="Linux"),
            mock.patch("arctl.doctor.shutil.which", return_value="/usr/bin/tool"),
            mock.patch(
                "arctl.doctor.inspect_process",
                return_value=ProcessIdentity("Linux", 1, 1, 1, "running"),
            ),
            mock.patch(
                "arctl.doctor._profile_probe", return_value=(True, None)
            ) as profile,
            mock.patch("arctl.doctor._process_cleanup_probe", return_value=True),
        ):
            report = run_doctor()

        self.assertTrue(doctor_succeeded(report), report)
        self.assertEqual(report["runtime"]["process_backend"], "procfs")
        self.assertEqual(report["runtime"]["sandbox_backend"], "bubblewrap")
        self.assertEqual(report["diagnostics"], {})
        self.assertEqual(profile.call_count, 3)
        self.assertEqual(
            [call.kwargs["profile"] for call in profile.call_args_list],
            ["arctl-research", "arctl-subject", "arctl-evaluator"],
        )

    def test_profile_probe_failure_is_reported_without_masking_cleanup(self) -> None:
        with (
            mock.patch("arctl.doctor.platform.system", return_value="Linux"),
            mock.patch("arctl.doctor.shutil.which", return_value="/usr/bin/tool"),
            mock.patch(
                "arctl.doctor.inspect_process",
                return_value=ProcessIdentity("Linux", 1, 1, 1, "running"),
            ),
            mock.patch(
                "arctl.doctor._profile_probe",
                return_value=(False, "sandbox unavailable"),
            ),
            mock.patch(
                "arctl.doctor._process_cleanup_probe",
                return_value=True,
            ) as cleanup,
        ):
            report = run_doctor()

        checks = report["checks"]
        self.assertFalse(checks["research_profile"])
        self.assertFalse(checks["subject_profile"])
        self.assertFalse(checks["evaluator_profile"])
        self.assertTrue(checks["timeout_child_cleanup"])
        self.assertEqual(report["diagnostics"]["research_profile"], "sandbox unavailable")
        cleanup.assert_called_once()

    def test_reports_supported_macos_backends(self) -> None:
        with (
            mock.patch("arctl.doctor.platform.system", return_value="Darwin"),
            mock.patch("arctl.doctor.platform.machine", return_value="arm64"),
            mock.patch("arctl.doctor.shutil.which", return_value="/usr/bin/tool"),
            mock.patch("arctl.doctor.Path.is_file", return_value=True),
            mock.patch(
                "arctl.doctor.inspect_process",
                return_value=ProcessIdentity("Darwin", 1, 1, 1, "running"),
            ),
            mock.patch("arctl.doctor._profile_probe", return_value=(True, None)),
            mock.patch("arctl.doctor._process_cleanup_probe", return_value=True),
        ):
            report = run_doctor()

        self.assertTrue(doctor_succeeded(report), report)
        self.assertEqual(report["runtime"]["system"], "Darwin")
        self.assertEqual(report["runtime"]["architecture"], "arm64")
        self.assertEqual(report["runtime"]["process_backend"], "libproc")
        self.assertEqual(report["runtime"]["sandbox_backend"], "seatbelt")

    def test_unsupported_platform_has_complete_failed_report(self) -> None:
        with (
            mock.patch("arctl.doctor.platform.system", return_value="Windows"),
            mock.patch("arctl.doctor.shutil.which", return_value="C:/tool"),
            mock.patch("arctl.doctor._profile_probe") as profile,
        ):
            report = run_doctor()

        self.assertFalse(doctor_succeeded(report))
        self.assertFalse(report["checks"]["supported_platform"])
        self.assertFalse(report["checks"]["process_backend"])
        self.assertFalse(report["checks"]["sandbox_backend"])
        self.assertIn("Linux and macOS", report["diagnostics"]["supported_platform"])
        profile.assert_not_called()
