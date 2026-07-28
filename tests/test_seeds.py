from __future__ import annotations

import itertools
import unittest

from arctl.errors import ValidationError
from arctl.seeds import derive_seed, new_master_seed


class SeedTests(unittest.TestCase):
    def test_master_seed_has_256_bits(self) -> None:
        self.assertEqual(len(new_master_seed()), 32)

    def test_derivation_is_stable_and_domain_separated(self) -> None:
        master = bytes(range(32))
        dimensions = itertools.product(
            range(2),
            ("calibration", "primary", "suspect"),
            ("champion", "candidate", "evaluator"),
            range(3),
        )
        seeds = {
            derive_seed(
                master,
                experiment_id=experiment,
                phase=phase,
                subject=subject,
                trial=trial,
            )
            for experiment, phase, subject, trial in dimensions
        }
        self.assertEqual(len(seeds), 54)
        self.assertEqual(
            derive_seed(
                master,
                experiment_id=7,
                phase="primary",
                subject="candidate",
                trial=3,
            ),
            derive_seed(
                master,
                experiment_id=7,
                phase="primary",
                subject="candidate",
                trial=3,
            ),
        )

    def test_rejects_unknown_domains_and_short_master(self) -> None:
        with self.assertRaises(ValidationError):
            derive_seed(
                b"short",
                experiment_id=0,
                phase="primary",
                subject="candidate",
                trial=0,
            )
        with self.assertRaisesRegex(ValidationError, "unknown seed phase"):
            derive_seed(
                b"x" * 32,
                experiment_id=0,
                phase="retry",
                subject="candidate",
                trial=0,
            )
