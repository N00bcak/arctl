"""Immutable private reservations for paired comparisons."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Literal, Mapping, Sequence

from .errors import StateError, ValidationError
from .seeds import derive_seed, new_master_seed
from .storage import atomic_write_json

ComparisonKind = Literal["primary", "suspect"]
_SUBJECTS = ("champion", "candidate")
_FIELDS = {
    "kind",
    "experiment_id",
    "champion",
    "candidate",
    "evaluator",
    "manifest",
    "trial_count",
    "master_seed",
    "trial_seeds",
    "schedule_hash",
    "subject_order",
    "commands",
    "process_ids",
}
_COMMAND_FIELDS = {"subject", "prepare", "score"}
_PROCESS_FIELDS = {"prepare", "champion", "candidate", "score"}


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label} must be a non-empty string")
    return value


def _commands(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, Mapping) or set(value) != _COMMAND_FIELDS:
        raise ValidationError("reservation commands have invalid fields")
    result: dict[str, tuple[str, ...]] = {}
    for name, command in value.items():
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(argument, str) or not argument for argument in command)
        ):
            raise ValidationError(f"reservation command {name} is invalid")
        result[name] = tuple(command)
    return result


@dataclass(frozen=True)
class ComparisonReservation:
    kind: ComparisonKind
    experiment_id: int
    champion: str
    candidate: str
    evaluator: str
    manifest: str
    trial_count: int
    master_seed: bytes
    trial_seeds: tuple[int, ...]
    schedule_hash: str
    subject_order: tuple[str, str]
    commands: Mapping[str, tuple[str, ...]]
    process_ids: Mapping[str, str]

    def to_private_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "experiment_id": self.experiment_id,
            "champion": self.champion,
            "candidate": self.candidate,
            "evaluator": self.evaluator,
            "manifest": self.manifest,
            "trial_count": self.trial_count,
            "master_seed": self.master_seed.hex(),
            "trial_seeds": list(self.trial_seeds),
            "schedule_hash": self.schedule_hash,
            "subject_order": list(self.subject_order),
            "commands": {name: list(command) for name, command in self.commands.items()},
            "process_ids": dict(self.process_ids),
        }

    @classmethod
    def from_private_json(cls, value: Any) -> ComparisonReservation:
        if not isinstance(value, Mapping) or set(value) != _FIELDS:
            raise ValidationError("reservation fields are invalid")
        kind = value["kind"]
        if kind not in ("primary", "suspect"):
            raise ValidationError("reservation kind is invalid")
        experiment_id = value["experiment_id"]
        trial_count = value["trial_count"]
        if (
            isinstance(experiment_id, bool)
            or not isinstance(experiment_id, int)
            or experiment_id <= 0
        ):
            raise ValidationError("reservation experiment_id must be positive")
        if (
            isinstance(trial_count, bool)
            or not isinstance(trial_count, int)
            or trial_count <= 0
        ):
            raise ValidationError("reservation trial_count must be positive")
        try:
            master = bytes.fromhex(value["master_seed"])
        except (TypeError, ValueError) as error:
            raise ValidationError("reservation master_seed is invalid") from error
        if len(master) != 32:
            raise ValidationError("reservation master_seed must contain 256 bits")

        expected_seeds = tuple(
            derive_seed(
                master,
                experiment_id=experiment_id,
                phase=kind,
                subject="evaluator",
                trial=trial,
            )
            for trial in range(trial_count)
        )
        seeds = value["trial_seeds"]
        if not isinstance(seeds, list) or tuple(seeds) != expected_seeds:
            raise ValidationError("reservation trial seeds do not match their derivation")
        order = value["subject_order"]
        if not isinstance(order, list) or sorted(order) != sorted(_SUBJECTS):
            raise ValidationError("reservation subject_order is invalid")
        expected_order = (
            _SUBJECTS
            if derive_seed(
                master,
                experiment_id=experiment_id,
                phase=kind,
                subject="evaluator",
                trial=trial_count,
            )
            % 2
            == 0
            else tuple(reversed(_SUBJECTS))
        )
        if tuple(order) != expected_order:
            raise ValidationError("reservation subject_order does not match its derivation")

        schedule_hash = value["schedule_hash"]
        if schedule_hash != _canonical_hash(
            {"trial_seeds": list(expected_seeds), "subject_order": list(expected_order)}
        ):
            raise ValidationError("reservation schedule hash is invalid")
        process_ids = value["process_ids"]
        if not isinstance(process_ids, Mapping) or set(process_ids) != _PROCESS_FIELDS:
            raise ValidationError("reservation process_ids have invalid fields")
        for name, identifier in process_ids.items():
            _nonempty_string(identifier, f"process_ids.{name}")

        return cls(
            kind=kind,
            experiment_id=experiment_id,
            champion=_nonempty_string(value["champion"], "champion"),
            candidate=_nonempty_string(value["candidate"], "candidate"),
            evaluator=_nonempty_string(value["evaluator"], "evaluator"),
            manifest=_nonempty_string(value["manifest"], "manifest"),
            trial_count=trial_count,
            master_seed=master,
            trial_seeds=expected_seeds,
            schedule_hash=schedule_hash,
            subject_order=tuple(order),
            commands=_commands(value["commands"]),
            process_ids=dict(process_ids),
        )


def reserve_comparison(
    path: Path,
    *,
    kind: ComparisonKind,
    experiment_id: int,
    champion: str,
    candidate: str,
    evaluator: str,
    manifest: str,
    trial_count: int,
    commands: Mapping[str, Sequence[str]],
    master_seed: bytes | None = None,
    excluded_seeds: Collection[int] = (),
) -> ComparisonReservation:
    if path.exists():
        raise StateError("comparison is already reserved and cannot be redrawn")
    master = new_master_seed() if master_seed is None else master_seed
    seed_list = [
        derive_seed(
            master,
            experiment_id=experiment_id,
            phase=kind,
            subject="evaluator",
            trial=trial,
        )
        for trial in range(trial_count)
    ]
    overlap = set(seed_list).intersection(excluded_seeds)
    if overlap:
        raise StateError("comparison seeds overlap a previously reserved seed domain")
    order = (
        list(_SUBJECTS)
        if derive_seed(
            master,
            experiment_id=experiment_id,
            phase=kind,
            subject="evaluator",
            trial=trial_count,
        )
        % 2
        == 0
        else list(reversed(_SUBJECTS))
    )
    value = {
        "kind": kind,
        "experiment_id": experiment_id,
        "champion": champion,
        "candidate": candidate,
        "evaluator": evaluator,
        "manifest": manifest,
        "trial_count": trial_count,
        "master_seed": master.hex(),
        "trial_seeds": seed_list,
        "schedule_hash": _canonical_hash(
            {"trial_seeds": seed_list, "subject_order": order}
        ),
        "subject_order": order,
        "commands": {name: list(command) for name, command in commands.items()},
        "process_ids": {
            name: f"{kind}-{name}"
            for name in ("prepare", "champion", "candidate", "score")
        },
    }
    reservation = ComparisonReservation.from_private_json(value)
    atomic_write_json(path, reservation.to_private_json())
    return reservation


def load_reservation(path: Path) -> ComparisonReservation:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("comparison has no valid reservation") from error
    try:
        return ComparisonReservation.from_private_json(value)
    except ValidationError as error:
        raise StateError("comparison has no valid reservation") from error
