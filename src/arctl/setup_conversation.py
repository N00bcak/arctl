"""Revisioned, human-owned setup decisions for guided setup."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaError
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from .errors import StateError, ValidationError
from .storage import atomic_write_json, atomic_write_text

MAX_QUESTIONS = 3
HUMAN_REQUIRED = frozenset({"objective", "outcome", "policy_boundary"})
CONTROLLER_RULE_IDS = (
    "comparison",
    "seeds",
    "calibration",
    "decision",
    "failure",
)
_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_LINE = re.compile(r"^lines?\s+(\d+)(?:-(\d+))?$", re.IGNORECASE)


def _object(properties: Mapping[str, Any], *, required: Sequence[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required if required is not None else properties),
        "properties": dict(properties),
    }


def batch_schema(
    *,
    revision: int | None = None,
    decision_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    repository_citation = _object(
        {
            "kind": {"type": "string", "const": "repository"},
            "path": text,
            "location": {"type": "string", "pattern": _LINE.pattern},
            "finding": text,
            "excerpt_sha256": {
                "type": ["string", "null"],
                "pattern": "^[0-9a-f]{64}$",
            },
        },
    )
    controller_citation = _object(
        {
            "kind": {"type": "string", "const": "controller"},
            "rule_id": {"type": "string", "enum": list(CONTROLLER_RULE_IDS)},
            "finding": text,
        }
    )
    citation = {"anyOf": [repository_citation, controller_citation]}
    option = _object(
        {
            "id": {"type": "string", "pattern": _ID.pattern},
            "label": text,
            "value": text,
            "consequence": text,
            "citations": {"type": "array", "minItems": 1, "items": citation},
        }
    )
    question = _object(
        {
            "id": {"type": "string", "pattern": _ID.pattern},
            "prompt": text,
            "why": text,
            "options": {
                "type": "array",
                "minItems": 2,
                "maxItems": 4,
                "items": option,
            },
            "recommended_option_id": {"type": "string", "pattern": _ID.pattern},
            "allow_custom": {"type": "boolean", "const": True},
        }
    )
    decision_refs: dict[str, Any] = {
        "type": "array",
        "items": {"type": "string", "pattern": _ID.pattern},
    }
    if decision_ids is not None:
        if decision_ids:
            decision_refs["items"] = {"type": "string", "enum": list(decision_ids)}
        else:
            decision_refs["maxItems"] = 0
    provenance = {
        "source": {
            "type": "string",
            "enum": ["human", "repository", "controller", "derived"],
        },
        "decision_refs": decision_refs,
        "citations": {"type": "array", "items": citation},
    }
    objective = _object(
        {
            "value": text,
            **provenance,
        }
    )
    policy = _object(
        {
            "editable_paths": {
                "type": "array",
                "minItems": 1,
                "items": _object(
                    {
                        "pattern": text,
                        "origin": {"type": "string", "enum": ["existing", "generated"]},
                    }
                ),
            },
            "rationale": text,
            **provenance,
        }
    )
    environment = _object(
        {
            "owner": {"type": "string", "enum": ["subject", "environment"]},
            "source_path": text,
            "entrypoint": text,
            "interface": text,
            "rationale": text,
            **provenance,
        }
    )
    outcome = _object(
        {
            "statistic": text,
            "direction": {"type": "string", "enum": ["higher", "lower"]},
            "unit": text,
            "aggregation": text,
            "extraction": text,
            "result_path": {
                "type": "array",
                "minItems": 1,
                "items": text,
            },
            **provenance,
        }
    )
    trial = _object(
        {
            "unit": text,
            "termination": text,
            "horizon": _object(
                {
                    "unit": text,
                    "limit": {"type": "integer", "minimum": 1},
                    "case_field": text,
                }
            ),
            "seed_handling": text,
            **provenance,
        }
    )
    derived = _object(
        {
            "hard_rules": {"type": "array", "items": text},
            "hidden_data": text,
            "telemetry": {"type": "array", "items": text},
            "runtime_limits": {"type": "array", "minItems": 1, "items": text},
            "evaluator_pattern": text,
        }
    )
    dependency = _object(
        {
            "requirement": text,
            "imports": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "pattern": r"^[A-Za-z_]\w*$"},
            },
            "reason": text,
            "origin": {"type": "string", "enum": ["repository", "proposed"]},
            "authorization_decision": {
                "type": ["string", "null"],
                "pattern": _ID.pattern,
            },
        }
    )
    design = _object(
        {
            "summary": text,
            "objective": objective,
            "policy": policy,
            "environment_adapter": environment,
            "outcome": outcome,
            "trial": trial,
            "derived_setup": derived,
            "conformance": _object(
                {
                    "seeded_variation": {"type": "boolean"},
                    "arm_symmetry": {
                        "type": "string",
                        "enum": ["antisymmetric", "not_applicable"],
                    },
                    "arm_symmetry_rationale": text,
                }
            ),
            "direct_dependencies": {"type": "array", "items": dependency},
        }
    )
    return _object(
        {
            "schema_version": {"type": "integer", "const": 1},
            "revision": (
                {"type": "integer", "const": revision}
                if revision is not None
                else {"type": "integer", "minimum": 1}
            ),
            "summary": text,
            "questions": {
                "type": "array",
                "maxItems": MAX_QUESTIONS,
                "items": question,
            },
            "design": {"anyOf": [{"type": "null"}, design]},
        }
    )


def finalized_design_schema() -> dict[str, Any]:
    design = batch_schema()["properties"]["design"]["anyOf"][1]
    properties = dict(design["properties"])
    text = {"type": "string", "minLength": 1}
    properties.update(
        {
            "schema_version": {"type": "integer", "const": 3},
            "revision": {"type": "integer", "minimum": 1},
            "decision_revision": {"type": "integer", "minimum": 1},
            "source_provenance": _object(
                {
                    "path": text,
                    "commit": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},
                }
            ),
            "controller_contract": _object(
                {
                    "version": {"type": "integer", "const": 1},
                    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                }
            ),
            "dependency_source_policy": _object(
                {
                    "index": text,
                    "fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                }
            ),
        }
    )
    return _object(properties)


def load_decisions(directory: Path) -> dict[str, Any]:
    path = directory / "setup" / "decisions.public.json"
    if not path.is_file():
        return {"schema_version": 1, "revision": 0, "decisions": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("saved setup decisions are invalid") from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("revision"), int)
        or not isinstance(value.get("decisions"), list)
    ):
        raise StateError("saved setup decisions are invalid")
    return value


def validate_batch(value: Mapping[str, Any], *, subject: Path, revision: int) -> dict[str, Any]:
    normalized = deepcopy(value)
    try:
        Draft202012Validator(batch_schema()).validate(normalized)
    except JsonSchemaError as error:
        raise ValidationError(f"setup question batch is invalid: {error.message}") from error
    if normalized["revision"] != revision:
        raise ValidationError("setup question batch has the wrong revision")
    questions = normalized["questions"]
    design = normalized["design"]
    if bool(questions) == (design is not None):
        raise ValidationError("setup batch must contain questions or one complete design")
    question_ids: set[str] = set()
    for question in questions:
        if question["id"] in question_ids:
            raise ValidationError("setup question IDs must be unique within a batch")
        question_ids.add(question["id"])
        option_ids = [option["id"] for option in question["options"]]
        if len(option_ids) != len(set(option_ids)):
            raise ValidationError(f"setup question {question['id']} has duplicate option IDs")
        if question["recommended_option_id"] not in option_ids:
            raise ValidationError(f"setup question {question['id']} recommends an unknown option")
        for option in question["options"]:
            _validate_citations(option["citations"], subject)
    if design is not None:
        for section in ("objective", "policy", "environment_adapter", "outcome", "trial"):
            _validate_citations(design[section]["citations"], subject)
            if design[section]["source"] in {"repository", "controller"} and not design[
                section
            ]["citations"]:
                raise ValidationError(
                    f"setup design section {section} lacks grounded support"
                )
        _validate_editable_paths(design["policy"]["editable_paths"])
        _validate_direct_dependencies(
            design["direct_dependencies"], subject=subject
        )
        for editable in design["policy"]["editable_paths"]:
            if editable["origin"] == "existing" and not any(
                path.is_file() for path in subject.glob(editable["pattern"])
            ):
                raise ValidationError(
                    "setup design existing editable path matches no file: "
                    + editable["pattern"]
                )
        adapter = design["environment_adapter"]
        adapter_relative = Path(adapter["source_path"])
        if (
            adapter_relative.is_absolute()
            or adapter_relative == Path(".")
            or ".." in adapter_relative.parts
            or re.fullmatch(r"\.[A-Za-z0-9]+", adapter_relative.suffix) is None
        ):
            raise ValidationError(
                "setup design environment adapter source_path must be one file path"
            )
        if adapter["owner"] == "subject":
            adapter_path = subject / adapter_relative
            if adapter_path.is_symlink() or (
                not adapter_path.is_file() and adapter["source"] != "derived"
            ):
                raise ValidationError(
                    "setup design subject adapter source does not exist: "
                    + adapter["source_path"]
                )
    return normalized


def _validate_citations(citations: Sequence[dict[str, Any]], subject: Path) -> None:
    for citation in citations:
        if citation["kind"] == "controller":
            if citation["rule_id"] not in CONTROLLER_RULE_IDS:
                raise ValidationError("setup citation names an unknown controller rule")
            continue
        path_text = citation["path"]
        relative = Path(path_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValidationError("setup citation path must be repository-relative")
        path = subject / relative
        if path.is_symlink() or not path.is_file() or subject.resolve() not in path.resolve().parents:
            raise ValidationError(f"setup citation path does not exist: {path_text}")
        matched = _LINE.fullmatch(citation["location"].strip())
        if matched is None:
            raise ValidationError(
                f"setup citation location must be 'line N' or 'lines N-M': {path_text}"
            )
        start = int(matched.group(1))
        end = int(matched.group(2) or start)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        line_count = len(lines)
        if start < 1 or end < start or end > max(line_count, 1):
            raise ValidationError(f"setup citation location is outside {path_text}")
        excerpt = "\n".join(lines[start - 1 : end]).encode()
        digest = hashlib.sha256(excerpt).hexdigest()
        supplied = citation.get("excerpt_sha256")
        if supplied is not None and supplied != digest:
            raise ValidationError(f"setup citation excerpt changed in {path_text}")
        citation["excerpt_sha256"] = digest


def _validate_editable_paths(paths: Sequence[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for item in paths:
        pattern = item["pattern"]
        relative = Path(pattern)
        if (
            relative.is_absolute()
            or relative == Path(".")
            or ".." in relative.parts
            or ".git" in relative.parts
            or pattern.startswith("_arctl/")
            or pattern in seen
        ):
            raise ValidationError(f"setup design has an unsafe editable path: {pattern}")
        seen.add(pattern)


def _validate_direct_dependencies(
    dependencies: Sequence[Mapping[str, Any]], *, subject: Path
) -> None:
    findings: list[str] = []
    names: set[str] = set()
    local_names = {
        canonicalize_name(path.name)
        for path in subject.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    for dependency in dependencies:
        requirement = dependency["requirement"]
        try:
            parsed = Requirement(requirement)
        except InvalidRequirement:
            findings.append(f"dependency is not valid PEP 508: {requirement!r}")
            continue
        if str(parsed) != requirement:
            findings.append(
                f"dependency must use canonical PEP 508 text {str(parsed)!r}"
            )
        name = canonicalize_name(parsed.name)
        if name in names:
            findings.append(f"dependency name is duplicated: {parsed.name}")
        names.add(name)
        if name in local_names and parsed.url is None:
            findings.append(
                f"dependency {parsed.name!r} is supplied by the subject tree"
            )
        imports = dependency["imports"]
        if len(imports) != len(set(imports)):
            findings.append(f"dependency {requirement!r} repeats an import name")
        if "cv2" in imports and name != "opencv-python-headless":
            findings.append(
                "the cv2 import must use opencv-python-headless; GUI OpenCV is not authorized"
            )
    if findings:
        raise ValidationError("; ".join(findings))


def _validate_dependency_decision_refs(
    design: Mapping[str, Any], decision_ids: set[str]
) -> None:
    findings: list[str] = []
    for dependency in design["direct_dependencies"]:
        decision = dependency["authorization_decision"]
        if decision is not None and decision not in decision_ids:
            findings.append(
                f"dependency {dependency['requirement']!r} references unknown decision {decision!r}"
            )
        if dependency["origin"] == "proposed" and decision is None:
            findings.append(
                f"proposed dependency {dependency['requirement']!r} requires an explicit decision"
            )
    if findings:
        raise ValidationError("; ".join(findings))


def save_batch(directory: Path, value: Mapping[str, Any]) -> None:
    atomic_write_json(directory / "setup" / "question-batch.public.json", value)


def answer_batch(
    directory: Path,
    setup: dict[str, Any],
    submission: Mapping[str, Any],
) -> dict[str, Any]:
    batch_path = directory / "setup" / "question-batch.public.json"
    try:
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("setup has no valid question batch") from error
    revision = submission.get("revision")
    if revision != batch.get("revision"):
        raise ValidationError("setup answers target a stale question batch")
    answers = submission.get("answers")
    if not isinstance(answers, Mapping):
        raise ValidationError("setup answers must contain one answers object")
    questions = {question["id"]: question for question in batch["questions"]}
    if set(answers) != set(questions):
        raise ValidationError("setup answers must resolve exactly the current question batch")
    decisions = load_decisions(directory)
    existing = {item["id"] for item in decisions["decisions"]}
    for identifier, raw in answers.items():
        question = questions[identifier]
        options = {option["id"]: option for option in question["options"]}
        option_id: str | None
        citations: list[dict[str, str]]
        if isinstance(raw, str):
            if raw not in options:
                raise ValidationError(f"setup answer for {identifier} names an unknown option")
            option_id = raw
            value = options[raw]["value"]
            citations = list(options[raw]["citations"])
        elif isinstance(raw, Mapping) and set(raw) == {"custom"}:
            custom = raw["custom"]
            if not isinstance(custom, str) or not custom.strip():
                raise ValidationError(f"custom setup answer for {identifier} is empty")
            option_id = None
            value = custom.strip()
            citations = []
        else:
            raise ValidationError(f"setup answer for {identifier} is invalid")
        record = {
            "id": identifier,
            "question": question["prompt"],
            "answer": value,
            "option_id": option_id,
            "source": "human",
            "citations": citations,
        }
        if identifier in existing:
            decisions["decisions"] = [
                record if item["id"] == identifier else item
                for item in decisions["decisions"]
            ]
        else:
            decisions["decisions"].append(record)
            existing.add(identifier)
    decisions["revision"] = batch["revision"]
    atomic_write_json(directory / "setup" / "decisions.public.json", decisions)
    setup["state"] = "DISCOVERY_REQUIRED"
    atomic_write_json(directory / "setup.json", setup)
    return decisions


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _design_token(design: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(design)).hexdigest()[:24]


def _index_binding() -> dict[str, str]:
    configured = os.environ.get("ARCTL_PACKAGE_INDEX", "https://pypi.org/simple")
    parsed = urlsplit(configured)
    # Keep a useful, credential-free identity for confirmation while binding
    # authorization to the exact configured value (including credentials).
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    identity = (
        urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        if parsed.scheme and parsed.netloc
        else configured
    )
    return {
        "index": identity,
        "fingerprint": hashlib.sha256(configured.encode()).hexdigest(),
    }


def _provenance_sections(design: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        design[name]
        for name in ("objective", "policy", "environment_adapter", "outcome", "trial")
    )


def finalize_design(
    directory: Path,
    setup: dict[str, Any],
    batch: Mapping[str, Any],
    *,
    controller_contract: Mapping[str, Any] | None = None,
) -> str:
    decisions = load_decisions(directory)
    ids = {decision["id"] for decision in decisions["decisions"]}
    missing = sorted(HUMAN_REQUIRED - ids)
    if missing:
        raise ValidationError(
            "setup design lacks explicit human decisions for: " + ", ".join(missing)
        )
    design = {
        **deepcopy(batch["design"]),
        "schema_version": 3,
        "revision": batch["revision"],
        "decision_revision": decisions["revision"],
        "source_provenance": {
            "path": setup["source_repo"],
            "commit": setup["source_commit"],
        },
        "controller_contract": {
            "version": 1,
            "sha256": hashlib.sha256(
                _canonical_bytes(controller_contract or {})
            ).hexdigest(),
        },
        "dependency_source_policy": _index_binding(),
    }
    decision_ids = ids
    _validate_dependency_decision_refs(design, decision_ids)
    for section in _provenance_sections(design):
        unknown = set(section["decision_refs"]) - decision_ids
        if unknown:
            raise ValidationError(
                "setup design references unknown decisions: "
                + ", ".join(sorted(unknown))
            )
    required_refs = {
        "objective": ("objective", design["objective"]["decision_refs"]),
        "outcome": ("outcome", design["outcome"]["decision_refs"]),
        "policy_boundary": ("policy_boundary", design["policy"]["decision_refs"]),
    }
    missing_refs = sorted(
        name for name, (expected, refs) in required_refs.items() if expected not in refs
    )
    if missing_refs:
        raise ValidationError(
            "setup design lacks human decision references for: " + ", ".join(missing_refs)
        )
    try:
        Draft202012Validator(finalized_design_schema()).validate(design)
    except JsonSchemaError as error:
        raise ValidationError(f"final setup design is invalid: {error.message}") from error
    atomic_write_json(directory / "setup" / "design.public.json", design)
    token = _design_token(design)
    setup["state"] = "DESIGN_AUTHORIZATION_REQUIRED"
    setup["design_authorization_token"] = token
    atomic_write_json(directory / "setup.json", setup)
    return token


def authorize_design(directory: Path, setup: dict[str, Any], token: str) -> None:
    if setup.get("state") != "DESIGN_AUTHORIZATION_REQUIRED":
        raise StateError("setup design is not awaiting authorization")
    try:
        design = json.loads(
            (directory / "setup" / "design.public.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(finalized_design_schema()).validate(design)
    except (OSError, json.JSONDecodeError, JsonSchemaError) as error:
        raise ValidationError("setup design awaiting authorization is invalid") from error
    try:
        _validate_direct_dependencies(
            design["direct_dependencies"], subject=Path(setup["subject"])
        )
        _validate_dependency_decision_refs(
            design,
            {
                item["id"]
                for item in load_decisions(directory)["decisions"]
                if isinstance(item, Mapping) and isinstance(item.get("id"), str)
            },
        )
    except ValidationError as error:
        setup["state"] = "DISCOVERY_REQUIRED"
        setup["prior_design_findings"] = [f"DESIGN_DEPENDENCY {error}"]
        setup.pop("design_authorization_token", None)
        atomic_write_json(directory / "setup.json", setup)
        raise ValidationError(
            "setup design dependency contract is invalid; discovery must revise it: "
            f"{error}"
        ) from error
    expected = _design_token(design)
    saved = setup.get("design_authorization_token")
    if (
        not isinstance(saved, str)
        or not hmac.compare_digest(token, saved)
        or not hmac.compare_digest(token, expected)
    ):
        raise ValidationError("setup design authorization token does not match current design")
    digest = hashlib.sha256(_canonical_bytes(design)).hexdigest()
    authorized_path = directory / "setup" / "authorized-design.public.json"
    authorization_path = directory / "setup" / "authorization.public.json"
    try:
        atomic_write_json(authorized_path, design)
        atomic_write_json(
            authorization_path,
            {
                "schema_version": 1,
                "design_sha256": digest,
                "decision_revision": design["decision_revision"],
                "authorized": True,
            },
        )
    except Exception:
        authorized_path.unlink(missing_ok=True)
        authorization_path.unlink(missing_ok=True)
        raise
    setup["state"] = "BUILD_REQUIRED"
    setup.pop("design_authorization_token", None)
    setup.pop("late_dependencies", None)
    setup.pop("prior_design_findings", None)
    atomic_write_json(directory / "setup.json", setup)
    (directory / "setup" / "late-dependencies.public.json").unlink(missing_ok=True)
    (directory / "setup" / "design-findings.public.json").unlink(missing_ok=True)


def render_setup_note(directory: Path, setup: Mapping[str, Any]) -> Path:
    destination = Path(setup["workspace"]) / "ARCTL_SETUP.md"
    decisions = load_decisions(directory)
    design = json.loads(
        (directory / "setup" / "authorized-design.public.json").read_text(encoding="utf-8")
    )
    if destination.exists():
        raise StateError(f"setup summary output already exists: {destination}")
    lines = [
        f"# ARCTL setup — {setup['task_id']}",
        "",
        "<!-- Generated by arctl. Structured setup state is canonical. -->",
        "",
        "## Confirmed choices",
        "",
    ]
    for decision in decisions["decisions"]:
        lines.append(f"- **{decision['id'].replace('_', ' ').title()}:** {decision['answer']}")
    lines.extend(["", "## Authorized setup", "", design["summary"], ""])
    lines.extend(
        [
            f"- **Objective:** {design['objective']['value']}",
            "- **Editable paths:** "
            + ", ".join(item["pattern"] for item in design["policy"]["editable_paths"]),
            f"- **Environment:** {design['environment_adapter']['entrypoint']} "
            f"({design['environment_adapter']['interface']})",
            f"- **Outcome:** {design['outcome']['statistic']} "
            f"({design['outcome']['direction']})",
            f"- **Trial:** {design['trial']['unit']}; maximum "
            f"{design['trial']['horizon']['limit']} {design['trial']['horizon']['unit']}",
            f"- **Evaluator:** {design['derived_setup']['evaluator_pattern']}",
        ]
    )
    atomic_write_text(destination, "\n".join(lines) + "\n")
    return destination
