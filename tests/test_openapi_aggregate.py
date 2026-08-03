"""Lossless, deterministic aggregate OpenAPI contracts."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.utils.openapi_aggregate import load_openapi_document, merge_openapi_documents


def _document(
    title: str,
    *,
    path: str,
    schema_value: dict,
    root_extension: str,
) -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": title, "version": "2026.08.02"},
        "servers": [{"url": "https://example.invalid"}],
        "security": [{"apiKeyAuth": []}],
        "tags": [{"name": "inventory", "description": title}],
        "paths": {
            path: {
                "post": {
                    "operationId": f"create{title}",
                    "security": [{"apiKeyAuth": []}],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Resource"}
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
        "components": {
            "schemas": {"Resource": schema_value},
            "securitySchemes": {
                "apiKeyAuth": {"type": "apiKey", "in": "header", "name": "Authorization"}
            },
        },
        "x-ves-proto-package": root_extension,
    }


def test_conflicting_components_are_namespaced_and_local_references_follow_source() -> None:
    alpha = _document(
        "Alpha",
        path="/alpha",
        schema_value={"type": "object", "properties": {"alpha": {"type": "string"}}},
        root_extension="alpha.package",
    )
    beta = _document(
        "Beta",
        path="/beta",
        schema_value={"type": "object", "properties": {"beta": {"type": "integer"}}},
        root_extension="beta.package",
    )

    forward = merge_openapi_documents(
        [("alpha.json", alpha), ("beta.json", beta)],
        title="Aggregate",
        version="2026.08.02-1",
    )
    reverse = merge_openapi_documents(
        [("beta.json", deepcopy(beta)), ("alpha.json", deepcopy(alpha))],
        title="Aggregate",
        version="2026.08.02-1",
    )

    assert forward == reverse
    variants = forward["x-f5-component-variants"]["schemas"]["Resource"]
    assert [variant["sources"] for variant in variants] == [["alpha.json"], ["beta.json"]]
    emitted_names = [variant["emitted_name"] for variant in variants]
    assert len(set(emitted_names)) == 2
    schemas = forward["components"]["schemas"]
    assert set(emitted_names) <= set(schemas)
    assert (
        forward["paths"]["/alpha"]["post"]["requestBody"]["content"]["application/json"]["schema"][
            "$ref"
        ]
        == f"#/components/schemas/{emitted_names[0]}"
    )
    assert (
        forward["paths"]["/beta"]["post"]["requestBody"]["content"]["application/json"]["schema"][
            "$ref"
        ]
        == f"#/components/schemas/{emitted_names[1]}"
    )


def test_conflicting_self_references_converge_on_their_own_variant() -> None:
    alpha = _document(
        "Alpha",
        path="/alpha",
        schema_value={
            "type": "object",
            "properties": {
                "kind": {"const": "alpha"},
                "next": {"$ref": "#/components/schemas/Resource"},
            },
        },
        root_extension="alpha.package",
    )
    beta = _document(
        "Beta",
        path="/beta",
        schema_value={
            "type": "object",
            "properties": {
                "kind": {"const": "beta"},
                "next": {"$ref": "#/components/schemas/Resource"},
            },
        },
        root_extension="beta.package",
    )

    merged = merge_openapi_documents(
        [("alpha.json", alpha), ("beta.json", beta)],
        title="Aggregate",
        version="2026.08.02-1",
    )

    variants = merged["x-f5-component-variants"]["schemas"]["Resource"]
    assert len(variants) == 2
    for variant in variants:
        emitted_name = variant["emitted_name"]
        schema = merged["components"]["schemas"][emitted_name]
        assert schema["properties"]["next"]["$ref"] == (f"#/components/schemas/{emitted_name}")


def test_conflicting_path_operations_are_preserved_as_sorted_source_variants() -> None:
    alpha = _document(
        "Alpha",
        path="/shared",
        schema_value={"type": "string"},
        root_extension="alpha.package",
    )
    beta = _document(
        "Beta",
        path="/shared",
        schema_value={"type": "integer"},
        root_extension="beta.package",
    )

    merged = merge_openapi_documents(
        [("beta.json", beta), ("alpha.json", alpha)],
        title="Aggregate",
        version="2026.08.02-1",
    )

    path_item = merged["paths"]["/shared"]
    assert set(path_item) == {"x-f5-path-variants"}
    variants = path_item["x-f5-path-variants"]
    assert [variant["source"] for variant in variants] == ["alpha.json", "beta.json"]
    assert [variant["value"]["post"]["operationId"] for variant in variants] == [
        "createAlpha",
        "createBeta",
    ]


def test_path_servers_override_differing_root_servers_without_operation_injection() -> None:
    alpha = _document(
        "Alpha",
        path="/alpha",
        schema_value={"type": "string"},
        root_extension="alpha.package",
    )
    beta = _document(
        "Beta",
        path="/beta",
        schema_value={"type": "string"},
        root_extension="beta.package",
    )
    alpha["servers"] = [{"url": "https://root-alpha.example.invalid"}]
    beta["servers"] = [{"url": "https://root-beta.example.invalid"}]
    alpha["paths"]["/alpha"]["servers"] = [{"url": "https://path-alpha.example.invalid"}]

    merged = merge_openapi_documents(
        [("alpha.json", alpha), ("beta.json", beta)],
        title="Aggregate",
        version="2026.08.02-1",
    )

    path_item = merged["paths"]["/alpha"]
    assert path_item["servers"] == [{"url": "https://path-alpha.example.invalid"}]
    assert "servers" not in path_item["post"]


@pytest.mark.parametrize(
    "ref",
    [
        "https://example.invalid/schemas.json#/Resource",
        "#/components/schemas",
        "#/components/schemas/Resource/properties/missing",
    ],
)
def test_external_malformed_and_dangling_references_fail_closed(ref: str) -> None:
    document = _document(
        "Alpha",
        path="/alpha",
        schema_value={"type": "string"},
        root_extension="alpha.package",
    )
    document["paths"]["/alpha"]["post"]["requestBody"]["content"]["application/json"]["schema"] = {
        "$ref": ref
    }

    with pytest.raises(ValueError, match=re.escape(ref)):
        merge_openapi_documents(
            [("alpha.json", document)],
            title="Aggregate",
            version="2026.08.02-1",
        )


def test_component_reference_uri_fragments_are_canonical_percent_encoded() -> None:
    document = _document(
        "Alpha",
        path="/alpha",
        schema_value={"type": "string"},
        root_extension="alpha.package",
    )
    schema = document["components"]["schemas"].pop("Resource")
    document["components"]["schemas"]["Foo Bar"] = schema
    reference = document["paths"]["/alpha"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]
    reference["$ref"] = "#/components/schemas/Foo%20Bar"

    merged = merge_openapi_documents(
        [("alpha.json", document)],
        title="Aggregate",
        version="2026.08.02-1",
    )
    emitted_ref = merged["paths"]["/alpha"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    assert emitted_ref == "#/components/schemas/Foo%20Bar"

    reference["$ref"] = "#/components/schemas/Foo Bar"
    with pytest.raises(ValueError, match="malformed local reference"):
        merge_openapi_documents(
            [("alpha.json", document)],
            title="Aggregate",
            version="2026.08.02-1",
        )


def test_identical_authentication_and_all_root_metadata_are_preserved() -> None:
    alpha = _document(
        "Alpha",
        path="/alpha",
        schema_value={"type": "string"},
        root_extension="alpha.package",
    )
    beta = _document(
        "Beta",
        path="/beta",
        schema_value={"type": "string"},
        root_extension="beta.package",
    )
    alpha["x-unknown-future-field"] = {"preserve": True}

    merged = merge_openapi_documents(
        [("alpha.json", alpha), ("beta.json", beta)],
        title="Aggregate",
        version="2026.08.02-1",
    )

    assert merged["security"] == [{"apiKeyAuth": []}]
    assert merged["components"]["securitySchemes"] == alpha["components"]["securitySchemes"]
    assert {tag["description"] for tag in merged["tags"]} == {"Alpha", "Beta"}
    metadata = merged["x-f5-domain-metadata"]
    assert metadata["alpha.json"]["x-ves-proto-package"] == "alpha.package"
    assert metadata["alpha.json"]["x-unknown-future-field"] == {"preserve": True}
    assert metadata["beta.json"]["info"]["title"] == "Beta"


def test_json_and_yaml_inputs_are_loaded_strictly(tmp_path: Path) -> None:
    json_path = tmp_path / "alpha.json"
    yaml_path = tmp_path / "beta.yaml"
    json_path.write_text(
        json.dumps(
            _document(
                "Alpha",
                path="/alpha",
                schema_value={"type": "string"},
                root_extension="alpha.package",
            )
        )
    )
    yaml_path.write_text("openapi: 3.0.0\ninfo:\n  title: Beta\n  version: '1'\npaths: {}\n")

    assert load_openapi_document(json_path)["info"]["title"] == "Alpha"
    assert load_openapi_document(yaml_path)["info"]["title"] == "Beta"


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("duplicate.json", '{"openapi":"3.0.0","paths":{},"paths":{}}'),
        ("nan.json", '{"openapi":"3.0.0","paths":{},"value":NaN}'),
        ("duplicate.yaml", "openapi: 3.0.0\npaths: {}\npaths: {}\n"),
    ],
)
def test_duplicate_keys_and_nonfinite_values_fail_closed(
    tmp_path: Path,
    name: str,
    content: str,
) -> None:
    path = tmp_path / name
    path.write_text(content)

    with pytest.raises(ValueError, match="duplicate|finite"):
        load_openapi_document(path)


def test_current_domain_corpus_preserves_every_source_path_and_component() -> None:
    specs_dir = Path(__file__).parents[1] / "release" / "specs"
    paths = sorted(
        path
        for path in specs_dir.iterdir()
        if path.suffix in {".json", ".yaml", ".yml"} and not path.name.startswith(".")
    )
    if not paths:
        pytest.skip("current reconciled domain corpus is not present")
    documents = [(path.name, load_openapi_document(path)) for path in paths]

    merged = merge_openapi_documents(
        documents,
        title="F5 Distributed Cloud API (Fixed)",
        version="2026.08.02-1",
    )

    assert len(merged["x-f5-domain-metadata"]) == len(paths)
    expected_paths = {key for _, document in documents for key in document.get("paths", {})}
    assert set(merged["paths"]) == expected_paths
    for category in {key for _, document in documents for key in document.get("components", {})}:
        expected = {
            key
            for _, document in documents
            for key in document.get("components", {}).get(category, {})
        }
        emitted = merged["components"].get(category, {})
        variants = merged.get("x-f5-component-variants", {}).get(category, {})
        represented = set(emitted) | set(variants)
        assert expected <= represented
