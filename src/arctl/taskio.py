"""Task and approved-manifest loading."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .errors import StateError, ValidationError
from .manifest import EvaluatorManifest
from .models import TaskConfig


def load_task(path: Path) -> TaskConfig:
    try:
        import yaml
    except ImportError as error:
        raise StateError("PyYAML is required to read task files; run install.sh") from error
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise StateError(f"cannot read task file: {path}") from error
    except yaml.YAMLError as error:
        raise ValidationError(f"task file is not valid YAML: {path}") from error
    if not isinstance(value, dict):
        raise ValidationError("task file must contain one mapping")
    return TaskConfig.from_mapping(value)


def load_manifest(path: Path) -> tuple[EvaluatorManifest, str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except OSError as error:
        raise StateError(f"cannot read evaluator manifest: {path}") from error
    except json.JSONDecodeError as error:
        raise ValidationError("evaluator manifest is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValidationError("evaluator manifest must contain one object")
    return EvaluatorManifest.from_mapping(value), hashlib.sha256(raw).hexdigest()
