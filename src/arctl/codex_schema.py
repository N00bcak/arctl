"""Validation for Codex strict structured-output schemas."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .errors import StateError

_SUPPORTED_KEYWORDS = {
    "$defs",
    "$ref",
    "additionalProperties",
    "anyOf",
    "const",
    "description",
    "enum",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "format",
    "items",
    "maxItems",
    "maxLength",
    "maximum",
    "minItems",
    "minLength",
    "minimum",
    "multipleOf",
    "pattern",
    "properties",
    "required",
    "title",
    "type",
}
_MAX_PROPERTIES = 5_000
_MAX_NESTING_DEPTH = 10
_MAX_ENUM_VALUES = 1_000
_MAX_SCHEMA_STRING_CHARACTERS = 120_000
_LARGE_ENUM_THRESHOLD = 250
_MAX_LARGE_ENUM_STRING_CHARACTERS = 15_000


def load_codex_output_schema(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"Codex output schema cannot be loaded: {path}") from error
    if not isinstance(value, Mapping):
        raise StateError("Codex output schema root must be an object")
    validate_codex_output_schema(value)
    return value


def validate_codex_output_schema(schema: Mapping[str, Any]) -> None:
    """Enforce the JSON Schema subset accepted by Codex structured outputs."""
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise StateError("Codex output schema is not valid JSON Schema") from error

    property_count = 0
    enum_count = 0
    schema_string_characters = 0

    def add_string(value: str) -> None:
        nonlocal schema_string_characters
        schema_string_characters += len(value)

    def visit(node: Mapping[str, Any], path: str, depth: int) -> None:
        nonlocal property_count, enum_count
        if depth > _MAX_NESTING_DEPTH:
            raise StateError(
                f"Codex output schema exceeds {_MAX_NESTING_DEPTH} nesting levels: {path}"
            )

        unsupported = set(node) - _SUPPORTED_KEYWORDS
        if unsupported:
            raise StateError(
                "Codex output schema uses unsupported keywords at "
                f"{path}: {sorted(unsupported)}"
            )

        reference = node.get("$ref")
        if reference is not None:
            if not isinstance(reference, str) or not reference.startswith("#"):
                raise StateError(f"Codex output schema reference is not local: {path}")

        types = node.get("type")
        type_names = {types} if isinstance(types, str) else set(types or ())
        alternatives = node.get("anyOf")
        if reference is None and alternatives is None and not type_names:
            raise StateError(f"Codex output schema node lacks a type: {path}")

        if alternatives is not None:
            if not isinstance(alternatives, list) or not alternatives:
                raise StateError(f"Codex output schema anyOf is invalid: {path}")
            for index, alternative in enumerate(alternatives):
                if not isinstance(alternative, Mapping):
                    raise StateError(f"Codex output schema anyOf is invalid: {path}")
                visit(alternative, f"{path}.anyOf[{index}]", depth + 1)

        if "object" in type_names:
            properties = node.get("properties")
            required = node.get("required")
            if (
                not isinstance(properties, Mapping)
                or node.get("additionalProperties") is not False
                or not isinstance(required, list)
                or len(required) != len(properties)
                or set(required) != set(properties)
            ):
                raise StateError(f"Codex output schema object is not strict: {path}")
            property_count += len(properties)
            if property_count > _MAX_PROPERTIES:
                raise StateError(
                    f"Codex output schema exceeds {_MAX_PROPERTIES} object properties"
                )
            for name, child in properties.items():
                if not isinstance(name, str) or not isinstance(child, Mapping):
                    raise StateError(
                        f"Codex output schema properties are invalid: {path}"
                    )
                add_string(name)
                visit(child, f"{path}.properties.{name}", depth + 1)

        if "array" in type_names:
            items = node.get("items")
            if not isinstance(items, Mapping):
                raise StateError(f"Codex output schema array lacks items: {path}")
            visit(items, f"{path}.items", depth + 1)

        enum = node.get("enum")
        if enum is not None:
            enum_count += len(enum)
            if enum_count > _MAX_ENUM_VALUES:
                raise StateError(
                    f"Codex output schema exceeds {_MAX_ENUM_VALUES} enum values"
                )
            enum_string_characters = sum(
                len(value) for value in enum if isinstance(value, str)
            )
            for value in enum:
                if isinstance(value, str):
                    add_string(value)
            if (
                len(enum) > _LARGE_ENUM_THRESHOLD
                and enum_string_characters > _MAX_LARGE_ENUM_STRING_CHARACTERS
            ):
                raise StateError(
                    "Codex output schema enum string data exceeds "
                    f"{_MAX_LARGE_ENUM_STRING_CHARACTERS} characters: {path}"
                )

        constant = node.get("const")
        if isinstance(constant, (list, Mapping)):
            raise StateError(
                f"Codex output schema const must be a scalar value: {path}"
            )
        if isinstance(constant, str):
            add_string(constant)

        definitions = node.get("$defs", {})
        if not isinstance(definitions, Mapping):
            raise StateError(f"Codex output schema definitions are invalid: {path}")
        for name, child in definitions.items():
            if not isinstance(name, str) or not isinstance(child, Mapping):
                raise StateError(f"Codex output schema definitions are invalid: {path}")
            add_string(name)
            visit(child, f"{path}.$defs.{name}", depth + 1)

    if schema.get("type") != "object" or "anyOf" in schema:
        raise StateError("Codex output schema root must be an object")
    visit(schema, "$", 1)
    if schema_string_characters > _MAX_SCHEMA_STRING_CHARACTERS:
        raise StateError(
            "Codex output schema property, definition, enum, and const strings exceed "
            f"{_MAX_SCHEMA_STRING_CHARACTERS} characters"
        )
