from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arctl.comparison import load_reservation, reserve_comparison
from arctl.errors import StateError

COMMANDS = {
    "subject": ("python3", "subject.py", "{input}", "{output}"),
    "prepare": ("python3", "evaluator.py", "{request}", "{response}"),
    "score": ("python3", "evaluator.py", "{request}", "{response}"),
}


class ComparisonReservationTests(unittest.TestCase):
    def reserve(self, path: Path, *, kind: str = "primary", master: bytes = b"x" * 32):
        return reserve_comparison(
            path,
            kind=kind,  # type: ignore[arg-type]
            experiment_id=7,
            champion="a" * 40,
            candidate="b" * 40,
            evaluator="c" * 40,
            manifest="d" * 64,
            trial_count=4,
            commands=COMMANDS,
            master_seed=master,
        )

    def test_round_trips_and_verifies_derived_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "reservation.private.json")
            reserved = self.reserve(path)
            loaded = load_reservation(path)
            self.assertEqual(loaded, reserved)
            self.assertEqual(len(set(loaded.trial_seeds)), 4)
            self.assertEqual(set(loaded.subject_order), {"champion", "candidate"})

    def test_never_overwrites_an_existing_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "reservation.private.json")
            first = self.reserve(path)
            original = path.read_bytes()
            with self.assertRaisesRegex(StateError, "cannot be redrawn"):
                self.reserve(path, master=b"y" * 32)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(load_reservation(path), first)

    def test_primary_and_suspect_domains_do_not_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = self.reserve(root / "primary.json")
            suspect = self.reserve(root / "suspect.json", kind="suspect")
            self.assertTrue(set(primary.trial_seeds).isdisjoint(suspect.trial_seeds))

    def test_experiment_domain_does_not_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.reserve(root / "first.json")
            second = reserve_comparison(
                root / "second.json",
                kind="primary",
                experiment_id=8,
                champion="a" * 40,
                candidate="e" * 40,
                evaluator="c" * 40,
                manifest="d" * 64,
                trial_count=4,
                commands=COMMANDS,
                master_seed=b"x" * 32,
            )
            self.assertTrue(set(first.trial_seeds).isdisjoint(second.trial_seeds))

    def test_rejects_overlap_with_previously_reserved_seed_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.reserve(root / "first.json", master=bytes(range(32)))
            overlap = root / "overlap.json"
            with self.assertRaisesRegex(StateError, "overlap"):
                reserve_comparison(
                    overlap,
                    kind="primary",
                    experiment_id=7,
                    champion="champion",
                    candidate="candidate",
                    evaluator="evaluator",
                    manifest="manifest",
                    trial_count=4,
                    commands=COMMANDS,
                    master_seed=bytes(range(32)),
                    excluded_seeds=set(first.trial_seeds),
                )
            self.assertFalse(overlap.exists())

    def test_detects_tampered_seed_order_and_hash(self) -> None:
        mutations = (
            lambda raw: raw["trial_seeds"].__setitem__(0, 0),
            lambda raw: raw.__setitem__("subject_order", list(reversed(raw["subject_order"]))),
            lambda raw: raw.__setitem__("schedule_hash", "0" * 64),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary, "reservation.private.json")
                    self.reserve(path)
                    raw = json.loads(path.read_text())
                    mutate(raw)
                    path.write_text(json.dumps(raw))
                    with self.assertRaisesRegex(StateError, "no valid reservation"):
                        load_reservation(path)
