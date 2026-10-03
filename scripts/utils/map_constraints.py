"""Lossless map-rule normalization and exact OpenAPI 3.0 projection."""

from __future__ import annotations

import copy
from typing import Any

PREFIX = "ves.io.schema.rules.map."
ALIASES = ("x-validation-rules", "x-ves-validation-rules")
RULES = {
    "max_pairs": ("cardinality", "maxProperties"),
    "min_pairs": ("cardinality", "minProperties"),
    "unique_values": ("crossEntry", "uniqueValues"),
    "keys.string.max_len": ("keys", "maxLength"),
    "keys.string.min_len": ("keys", "minLength"),
    "keys.string.pattern": ("keys", "pattern"),
    "keys.string.ip": ("keys", "format"),
    "keys.string.mac": ("keys", "format"),
    "keys.uint32.gte": ("keys", "minimum"),
    "keys.uint32.lte": ("keys", "maximum"),
    "keys.uint32.ranges": ("keys", "ranges"),
    "values.string.max_len": ("values", "maxLength"),
    "values.string.min_len": ("values", "minLength"),
    "values.string.pattern": ("values", "pattern"),
    "values.string.regex": ("values", "format"),
    "values.string.ipv4": ("values", "format"),
    "values.string.ipv6": ("values", "format"),
    "values.string.k8s_label_value": ("values", "format"),
    "values.string.uri_ref": ("values", "format"),
}
FORMATS = {
    "ip": "ip-address",
    "mac": "mac-address",
    "regex": "regex",
    "ipv4": "ipv4",
    "ipv6": "ipv6",
    "k8s_label_value": "k8s-label-value",
    "uri_ref": "uri-reference",
}
NATIVE_VALUE_KEYS = frozenset({"minLength", "maxLength", "pattern"})


def map_rules(node: dict[str, Any]) -> dict[str, Any]:
    """Merge vendor aliases, failing on contradictory declarations."""
    rules: dict[str, Any] = {}
    for alias in ALIASES:
        for key, value in node.get(alias, {}).items():
            if not key.startswith(PREFIX):
                continue
            if key in rules and rules[key] != value:
                raise ValueError(f"Conflicting map rule alias: {key}")
            rules[key] = value
    return dict(sorted(rules.items()))


def normalize_map(node: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize every observed family without losing source values."""
    raw = map_rules(node)
    if not raw:
        return None
    result: dict[str, Any] = {
        "constraintType": "map",
        "category": "discovery",
        "deterministic": True,
        "originalRules": raw,
    }
    for rule, value in raw.items():
        suffix = rule[len(PREFIX) :]
        if suffix not in RULES:
            raise ValueError(f"Unsupported map rule needs a disposition: {rule}")
        scope, key = RULES[suffix]
        if key in {
            "minLength",
            "maxLength",
            "minimum",
            "maximum",
            "minProperties",
            "maxProperties",
        }:
            normalized: Any = int(value)
            if normalized < 0:
                raise ValueError(f"Negative map bound: {rule}")
        elif key == "ranges":
            normalized = []
            for token in str(value).split(","):
                ends = token.strip().split("-")
                lo, hi = int(ends[0]), int(ends[-1])
                if lo < 0 or hi < lo or hi > 4294967295:
                    raise ValueError(f"Invalid uint32 map range: {value}")
                normalized.append([lo, hi])
        elif key in {"format", "uniqueValues"}:
            if value not in {True, False, "true", "false"}:
                raise ValueError(f"Invalid map boolean: {rule}")
            if value in {False, "false"}:
                continue
            normalized = FORMATS[suffix.split(".")[-1]] if key == "format" else True
        else:
            normalized = value
        group = result.setdefault(scope, {})
        if key in group and group[key] != normalized:
            raise ValueError(f"Conflicting normalized map semantics: {rule}")
        group[key] = normalized
        if scope == "values":
            group["type"] = "string"
        elif scope == "keys":
            group["type"] = "uint32-string" if ".uint32." in suffix else "string"
    return result


def _compatible_string(
    node: dict[str, Any], schemas: dict[str, Any], active: frozenset[str]
) -> bool:
    """Require all typed/ref/composed alternatives to describe string values."""
    if "$ref" in node:
        ref = node["$ref"]
        if ref in active or not ref.startswith("#/components/schemas/"):
            return False
        target = schemas.get(ref.rsplit("/", 1)[-1])
        return isinstance(target, dict) and _compatible_string(target, schemas, active | {ref})
    if node.get("type") not in {None, "string"}:
        return False
    groups = [node[key] for key in ("allOf", "oneOf", "anyOf") if key in node]
    return all(_compatible_string(member, schemas, active) for group in groups for member in group)


def project_map(node: dict[str, Any], schemas: dict[str, Any]) -> list[str]:
    """Project exact native bounds; return explicit extension-only reasons."""
    normalized = normalize_map(node)
    if normalized is None:
        return []
    reasons = []
    for key, value in normalized.get("cardinality", {}).items():
        if key in node and node[key] != value:
            raise ValueError(f"Conflicting native map bound: {key}")
        node[key] = value
    values = normalized.get("values")
    if values:
        current = node.get("additionalProperties", {})
        if current is True:
            current = {}
        if not isinstance(current, dict) or not _compatible_string(current, schemas, frozenset()):
            reasons.append("Map values are incompatible with native string projection")
        else:
            value_schema = copy.deepcopy(current)
            overlay = {key: value for key, value in values.items() if key in NATIVE_VALUE_KEYS}
            # OAS3 format validation varies; URI-reference and vendor formats remain explicit metadata.
            if not any(key in value_schema for key in ("$ref", "allOf", "oneOf", "anyOf")):
                for key, value in overlay.items():
                    if key in value_schema and value_schema[key] != value:
                        raise ValueError(f"Conflicting native map value bound: {key}")
                value_schema.update({"type": "string", **overlay})
            elif overlay:
                addition = {"type": "string", **overlay}
                members = value_schema.get("allOf", [])
                if addition not in members:
                    value_schema = {"allOf": [value_schema, addition]}
            node["additionalProperties"] = value_schema
        if "format" in values:
            reasons.append("Value format remains extension-only in the OpenAPI 3.0 contract")
    if "keys" in normalized:
        reasons.append("OpenAPI 3.0 cannot express property-name validation")
    if "crossEntry" in normalized:
        reasons.append("Object value uniqueness has no native OpenAPI 3.0 equivalent")
    return reasons


def schema_nodes(spec: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Visit physical schema locations, including inline requests and map values."""
    nodes: list[tuple[str, dict[str, Any]]] = []
    seen: set[int] = set()

    def visit(node: Any, path: str) -> None:
        if not isinstance(node, dict) or id(node) in seen:
            return
        seen.add(id(node))
        nodes.append((path, node))
        for name, child in node.get("properties", {}).items():
            visit(child, f"{path}/properties/{name}")
        for key in ("items", "additionalProperties", "not"):
            visit(node.get(key), f"{path}/{key}")
        for key in ("allOf", "oneOf", "anyOf"):
            for index, child in enumerate(node.get(key, [])):
                visit(child, f"{path}/{key}/{index}")

    def containers(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key == "schema":
                    visit(child, f"{path}/schema")
                else:
                    containers(child, f"{path}/{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                containers(child, f"{path}/{index}")

    for name, schema in sorted(spec.get("components", {}).get("schemas", {}).items()):
        visit(schema, f"#/components/schemas/{name}")
    containers(spec.get("paths", {}), "#/paths")
    return nodes
