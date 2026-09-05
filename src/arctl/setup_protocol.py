"""Controller-owned entrypoints copied into guided Python workspaces."""

from __future__ import annotations


SETUP_API_MODULE = '''\
"""Canonical arctl setup hook types. Do not change this controller-owned file."""

from __future__ import annotations

from typing import Any, TypedDict

JsonObject = dict[str, Any]


class PrepareContext(TypedDict):
    kind: str
    experiment_id: int
    trial_count: int
    trial_seeds: list[int]


class PreparedBatch(TypedDict):
    public_batch: JsonObject
    private_scoring: JsonObject


class CalibrationAssessment(TypedDict):
    trial_count: int
    diagnostic_value: float


class CalibrationContext(TypedDict):
    champion: str
    evaluator: str
    manifest: str
    policy: str
    ladder: list[int]
    diagnostic: JsonObject
    champion_output: JsonObject


class ScoreContext(TypedDict):
    kind: str
    experiment_id: int
    trial_count: int
    private_scoring: JsonObject
    champion_output: JsonObject
    candidate_output: JsonObject


class PairedTelemetry(TypedDict):
    champion: float
    candidate: float


class ComparisonTelemetry(TypedDict):
    value: float | bool


class ScoreAssessment(TypedDict):
    hard_rules_pass: bool
    effect_estimate: float
    one_sided_lower_bound: float
    suspect_required: bool
    suspect_reason: str | None
    telemetry: dict[str, PairedTelemetry | ComparisonTelemetry]
'''


SUBJECT_ENTRYPOINT = '''\
"""Controller-owned arctl subject protocol entrypoint."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _arctl.hook import run_batch


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: subject.py INPUT.json OUTPUT.json")
    source, destination = map(Path, sys.argv[1:])
    batch = json.loads(source.read_text(encoding="utf-8"))
    if (
        not isinstance(batch, dict)
        or set(batch) != {"trial_count", "cases"}
        or not isinstance(batch["trial_count"], int)
        or isinstance(batch["trial_count"], bool)
        or batch["trial_count"] < 1
        or not isinstance(batch["cases"], list)
        or len(batch["cases"]) != batch["trial_count"]
    ):
        raise ValueError("subject input batch has an invalid controller envelope")
    result = run_batch({"cases": batch["cases"]})
    if not isinstance(result, dict):
        raise TypeError("run_batch must return one JSON object")
    if set(result) != {"results"}:
        raise ValueError("run_batch must return one canonical results shape")
    results = result["results"]
    if not isinstance(results, list):
        raise ValueError("run_batch results must be a list")
    if len(results) != batch["trial_count"]:
        raise ValueError("run_batch result count differs from trial_count")
    destination.write_text(json.dumps({
        "trial_count": batch["trial_count"],
        "results": results,
    }, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
'''


UNITTEST_ENTRYPOINT = '''\
"""Controller-owned generated-setup test runner that rejects skipped coverage."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: unittest_runner.py START_DIRECTORY")
    start = Path(sys.argv[1]).resolve()
    suite = unittest.defaultTestLoader.discover(str(start))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    incomplete = bool(
        result.skipped
        or result.expectedFailures
        or result.unexpectedSuccesses
        or result.testsRun == 0
    )
    raise SystemExit(0 if result.wasSuccessful() and not incomplete else 1)


if __name__ == "__main__":
    main()
'''


EVALUATOR_ENTRYPOINT = '''\
"""Controller-owned arctl evaluator protocol entrypoint."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _arctl.hook import calibrate, prepare, score


def _object(value, label):
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be one JSON object")
    return value


def _read(path):
    return _object(json.loads(Path(path).read_text(encoding="utf-8")), str(path))


def _write(path, value):
    _object(value, "hook response")
    Path(path).write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _subject_results(path, expected_count=None):
    output = _read(path)
    if set(output) != {"trial_count", "results"}:
        raise ValueError("subject output has an invalid controller envelope")
    if expected_count is not None and output["trial_count"] != expected_count:
        raise ValueError("subject output trial count differs from request")
    results = output["results"]
    if not isinstance(results, list):
        raise ValueError("subject output results must be a list")
    if expected_count is not None and len(results) != expected_count:
        raise ValueError("subject output result count differs from request")
    return {"results": results}


def _prepare(request, response):
    context = {
        key: request[key]
        for key in ("kind", "experiment_id", "trial_count", "trial_seeds")
    }
    result = _object(prepare(context), "prepare result")
    if set(result) != {"public_batch", "private_scoring"}:
        raise ValueError("prepare must return public_batch and private_scoring")
    public_batch = _object(result["public_batch"], "prepared public batch")
    if set(public_batch) != {"cases"}:
        raise ValueError("prepare public_batch has an invalid shape")
    cases = public_batch["cases"]
    if not isinstance(cases, list):
        raise ValueError("prepare public cases must be a list")
    if len(cases) != request["trial_count"]:
        raise ValueError("prepare public case count differs from trial_count")
    _write(request["public_batch"], {
        "trial_count": request["trial_count"],
        "cases": cases,
    })
    _write(request["private_scoring"], result["private_scoring"])
    _write(response, {
        "operation": "prepare",
        "kind": request["kind"],
        "trial_count": request["trial_count"],
    })


def _calibrate(request, response):
    context = {
        key: request[key]
        for key in (
            "champion", "evaluator", "manifest", "policy", "ladder", "diagnostic"
        )
    }
    context["champion_output"] = _subject_results(request["champion_output"])
    assessments = calibrate(context)
    if not isinstance(assessments, list):
        raise TypeError("calibrate must return a list of assessments")
    _write(response, {
        "operation": "calibrate",
        "champion": request["champion"],
        "evaluator": request["evaluator"],
        "manifest": request["manifest"],
        "policy": request["policy"],
        "assessments": assessments,
    })


def _score(request, response):
    context = {
        key: request[key]
        for key in ("kind", "experiment_id", "trial_count")
    }
    context.update({
        "private_scoring": _read(request["private_scoring"]),
        "champion_output": _subject_results(
            request["champion_output"], request["trial_count"]
        ),
        "candidate_output": _subject_results(
            request["candidate_output"], request["trial_count"]
        ),
    })
    result = _object(score(context), "score result")
    expected = {
        "hard_rules_pass", "effect_estimate", "one_sided_lower_bound",
        "suspect_required", "suspect_reason", "telemetry",
    }
    if set(result) != expected:
        raise ValueError("score returned fields differing from its typed contract")
    _write(response, {
        "kind": request["kind"],
        "trial_count": request["trial_count"],
        "hard_rules_pass": result["hard_rules_pass"],
        "comparison": {
            "effect_estimate": result["effect_estimate"],
            "one_sided_lower_bound": result["one_sided_lower_bound"],
        },
        "suspect_test": {
            "required": result["suspect_required"],
            "reason": result["suspect_reason"],
        },
        "telemetry": result["telemetry"],
    })


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] not in {"prepare", "calibrate", "score"}:
        raise SystemExit("usage: evaluator.py OPERATION REQUEST.json RESPONSE.json")
    operation = sys.argv[1]
    request = _read(sys.argv[2])
    if request.get("operation") != operation:
        raise ValueError("request operation differs from command")
    {"prepare": _prepare, "calibrate": _calibrate, "score": _score}[operation](
        request, sys.argv[3]
    )


if __name__ == "__main__":
    main()
'''
