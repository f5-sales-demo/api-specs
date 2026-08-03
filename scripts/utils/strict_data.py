"""Fail-closed JSON, YAML, and archive-path primitives."""

from __future__ import annotations

import json
import math
from pathlib import PurePosixPath
from typing import Any

import yaml


class StrictDataError(ValueError):
    """Raised when input parsing would lose or reinterpret data."""


def _construct_unique_mapping(
    loader: yaml.SafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as error:
            raise StrictDataError("YAML mapping key is not hashable") from error
        if duplicate:
            raise StrictDataError(f"duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


def _reject_duplicate_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictDataError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise StrictDataError(f"JSON value must be finite: {value}")


def validate_json_data(value: Any, location: str = "document") -> None:
    """Require a finite JSON-compatible tree with string object keys."""
    if isinstance(value, float) and not math.isfinite(value):
        raise StrictDataError(f"{location} contains a non-finite number")
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise StrictDataError(f"{location} contains a non-string mapping key")
        for key, child in value.items():
            validate_json_data(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            validate_json_data(child, f"{location}[{index}]")


def strict_json_loads(content: str | bytes, location: str) -> Any:
    """Parse JSON while rejecting duplicate keys and non-finite numbers."""
    try:
        document = json.loads(
            content,
            object_pairs_hook=_reject_duplicate_json,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrictDataError(f"{location} is not valid JSON") from error
    validate_json_data(document, location)
    return document


def strict_yaml_loads(content: str | bytes, location: str) -> Any:
    """Parse safe YAML while rejecting duplicate keys and non-finite numbers."""
    loader = yaml.SafeLoader(content)
    loader.yaml_constructors = loader.yaml_constructors.copy()
    loader.yaml_constructors[yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG] = (
        _construct_unique_mapping
    )
    try:
        document = loader.get_single_data()
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise StrictDataError(f"{location} is not valid YAML") from error
    finally:
        loader.dispose()
    validate_json_data(document, location)
    return document


def canonical_json_bytes(value: Any) -> bytes:
    """Render one validated JSON tree to its canonical byte identity."""
    validate_json_data(value)
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError) as error:
        raise StrictDataError("value is not canonical JSON data") from error


def canonical_posix_path(name: str) -> PurePosixPath:
    """Return a path only when its spelling has one unambiguous ZIP identity."""
    path = PurePosixPath(name)
    invalid = (
        not name,
        "\\" in name,
        "\x00" in name,
        path.is_absolute(),
        path.as_posix() != name,
        any(part in {"", ".", ".."} for part in path.parts),
    )
    if any(invalid):
        raise StrictDataError(f"path is not canonical: {name!r}")
    return path
