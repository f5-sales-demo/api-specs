"""Contracts for synthetic OpenAPI identifier examples and their release gate."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.example_identifier_safety import (
    IdentifierPolicy,
    find_unsafe_identifiers,
    sanitize_identifier_examples,
)


def test_sanitizer_replaces_uuid_and_private_address_examples_deterministically() -> None:
    spec = {
        "description": "Resource 123e4567-e89b-42d3-a456-426614174000 at 10.24.0.8/16",
        "x-ves-example": "172.16.2.9",
    }

    sanitized = sanitize_identifier_examples(spec)

    assert sanitized == {
        "description": "Resource 00000000-0000-4000-8000-320159ebe321 at 192.0.2.0/24",
        "x-ves-example": "192.0.2.58",
    }
    assert sanitize_identifier_examples(sanitized) == sanitized


def test_release_gate_rejects_realistic_identifiers_but_allows_documented_synthetic_values() -> (
    None
):
    spec = {
        "description": "123e4567-e89b-42d3-a456-426614174000 10.24.0.8",
        "safe": "00000000-0000-4000-8000-320159ebe321 192.0.2.8",
    }

    findings = find_unsafe_identifiers(spec, IdentifierPolicy())

    assert {(finding.category, finding.path) for finding in findings} == {
        ("realistic-uuid", "/description"),
        ("private-ipv4", "/description"),
    }


def test_release_gate_allows_only_configured_schema_constant_paths() -> None:
    spec = {
        "components": {
            "schemas": {
                "example": {
                    "properties": {
                        "semantic_constant": {"default": "10.24.0.8"},
                        "unreviewed": {"default": "10.24.0.9"},
                    }
                }
            }
        }
    }
    policy = IdentifierPolicy(
        schema_constant_paths=frozenset(
            {"/components/schemas/example/properties/semantic_constant/default"}
        )
    )

    findings = find_unsafe_identifiers(spec, policy)

    assert [(finding.category, finding.path) for finding in findings] == [
        (
            "private-ipv4",
            "/components/schemas/example/properties/unreviewed/default",
        )
    ]


def test_committed_release_specs_pass_the_identifier_release_gate() -> None:
    policy = IdentifierPolicy()
    findings = []
    for spec_path in sorted(Path("release/specs").glob("*.json")):
        findings.extend(find_unsafe_identifiers(json.loads(spec_path.read_text()), policy))

    assert findings == []
