from __future__ import annotations

import unittest
from unittest import mock

from arctl.doctor import run_doctor


class DoctorTests(unittest.TestCase):
    def test_missing_prerequisite_short_circuits_privileged_probes(self) -> None:
        def which(name: str) -> str | None:
            return None if name == "codex" else f"/usr/bin/{name}"

        with (
            mock.patch("arctl.doctor.platform.system", return_value="Linux"),
            mock.patch("arctl.doctor.shutil.which", side_effect=which),
            mock.patch("arctl.doctor._profile_probe") as profile,
            mock.patch("arctl.doctor._process_cleanup_probe") as cleanup,
        ):
            checks = run_doctor()

        self.assertFalse(checks["codex"])
        profile.assert_not_called()
        cleanup.assert_not_called()

    def test_reports_each_isolation_profile_and_process_cleanup(self) -> None:
        with (
            mock.patch("arctl.doctor.platform.system", return_value="Linux"),
            mock.patch("arctl.doctor.shutil.which", return_value="/usr/bin/tool"),
            mock.patch("arctl.doctor._profile_probe", return_value=True) as profile,
            mock.patch("arctl.doctor._process_cleanup_probe", return_value=True),
        ):
            checks = run_doctor()

        self.assertTrue(all(checks.values()), checks)
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
                "arctl.doctor._profile_probe",
                side_effect=OSError("sandbox unavailable"),
            ),
            mock.patch(
                "arctl.doctor._process_cleanup_probe",
                return_value=True,
            ) as cleanup,
        ):
            checks = run_doctor()

        self.assertFalse(checks["research_profile"])
        self.assertFalse(checks["subject_profile"])
        self.assertFalse(checks["evaluator_profile"])
        self.assertTrue(checks["timeout_child_cleanup"])
        cleanup.assert_called_once()
