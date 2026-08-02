"""Measured nullable response corrections for OpenAPI 3.0 schemas."""

from __future__ import annotations

from typing import Any


def apply_nullable_response_corrections(
    spec: dict[str, Any],
    corrections: list[dict[str, Any]],
    filename: str,
) -> dict[str, Any]:
    """Allow measured response properties to carry JSON ``null``.

    OpenAPI 3.0 Reference Objects cannot have siblings. A nullable property
    that originally contains only ``$ref`` is therefore converted into a
    Schema Object with ``nullable`` and an ``allOf`` wrapper around the
    reference. Exact targets fail closed when upstream structure changes.
    """
    schemas = spec.get("components", {}).get("schemas", {})

    for rule in corrections:
        if rule["file_pattern"] not in filename:
            continue

        schema_name = rule["schema"]
        schema = schemas.get(schema_name)
        if not isinstance(schema, dict):
            raise ValueError(
                f"nullable response correction schema is missing: {filename}:{schema_name}"
            )
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            raise ValueError(
                f"nullable response correction has no properties: {filename}:{schema_name}"
            )

        for property_name in rule["properties"]:
            property_schema = properties.get(property_name)
            if not isinstance(property_schema, dict):
                raise ValueError(
                    "nullable response correction property is missing: "
                    f"{filename}:{schema_name}.{property_name}"
                )
            if property_schema.get("nullable") is True:
                continue
            properties[property_name] = {
                "nullable": True,
                "allOf": [property_schema],
            }

    return spec
