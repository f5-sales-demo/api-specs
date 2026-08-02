"""Measured nullable response corrections for the live API contract."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from scripts.transform import TransformConfig, mark_nullable_response_fields


def _config() -> TransformConfig:
    return TransformConfig(
        metadata={
            "nullable_response_corrections": [
                {
                    "file_pattern": ".schema.api_group.ves-swagger.json",
                    "schema": "api_groupListResponseItem",
                    "properties": ["get_spec", "metadata", "owner_view", "system_metadata"],
                }
            ]
        }
    )


def _spec() -> dict:
    return {
        "components": {
            "schemas": {
                "api_groupListResponseItem": {
                    "type": "object",
                    "properties": {
                        name: {"$ref": f"#/components/schemas/{name}"}
                        for name in ("get_spec", "metadata", "owner_view", "system_metadata")
                    },
                }
            }
        }
    }


def test_nullable_correction_wraps_references_without_ref_siblings() -> None:
    transformed = mark_nullable_response_fields(
        _spec(),
        _config(),
        "docs-cloud-f5-com.0004.public.ves.io.schema.api_group.ves-swagger.json",
    )

    properties = transformed["components"]["schemas"]["api_groupListResponseItem"]["properties"]
    for name in ("get_spec", "metadata", "owner_view", "system_metadata"):
        assert properties[name] == {
            "nullable": True,
            "allOf": [{"$ref": f"#/components/schemas/{name}"}],
        }


def test_nullable_correction_is_idempotent() -> None:
    filename = "docs-cloud-f5-com.0004.public.ves.io.schema.api_group.ves-swagger.json"
    first = mark_nullable_response_fields(_spec(), _config(), filename)
    second = mark_nullable_response_fields(first, _config(), filename)

    assert second == first


def test_committed_artifact_contains_measured_nullable_response_contract() -> None:
    root = Path(__file__).parents[1]
    corrections = yaml.safe_load(
        (root / "config" / "nullable_response_corrections.yaml").read_text()
    )["corrections"]

    for correction in corrections:
        matches = list((root / "release" / "specs").glob(f"*{correction['file_pattern']}"))
        assert len(matches) == 1
        spec = json.loads(matches[0].read_text())
        properties = spec["components"]["schemas"][correction["schema"]]["properties"]
        for name in correction["properties"]:
            assert properties[name]["nullable"] is True
            assert len(properties[name]["allOf"]) == 1
            assert set(properties[name]["allOf"][0]) == {"$ref"}
