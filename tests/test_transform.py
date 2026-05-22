"""Tests for the OAS3 transform pipeline."""

from __future__ import annotations

import json

import pytest

from scripts.transform import (
    HTTP_METHODS,
    TRANSFORM_REGISTRY,
    SpecTransformer,
    TransformConfig,
    deduplicate_operation_ids,
    fix_invalid_examples,
    inject_contact,
    inject_info_version,
    inject_operation_descriptions,
    inject_operation_tags,
    inject_security_schemes,
    inject_servers,
    mark_deprecated_operations,
    remove_deprecated_paths,
    remove_unused_schemas,
    rename_colliding_schemas,
    strip_script_tags,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "Test", "version": ""},
        "paths": {},
    }


@pytest.fixture
def transformer(tmp_path, minimal_spec):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()

    spec_file = input_dir / "test.json"
    spec_file.write_text(json.dumps(minimal_spec))

    config = TransformConfig(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        transforms={},
        spectral_config={},
        reconciliation_config={},
        metadata={},
    )
    return SpecTransformer(config)


@pytest.fixture
def config_with_metadata() -> TransformConfig:
    return TransformConfig(
        input_dir=".",
        output_dir=".",
        transforms={name: True for name, _ in TRANSFORM_REGISTRY},
        spectral_config={
            "contact": {
                "name": "F5 Distributed Cloud",
                "url": "https://docs.cloud.f5.com",
                "email": "support@f5.com",
            },
            "servers": [
                {
                    "url": "https://{tenant}.console.ves.volterra.io",
                    "description": "F5 Distributed Cloud API",
                    "variables": {"tenant": {"default": "example-tenant"}},
                },
            ],
            "security_scheme": {
                "type": "apiKey",
                "in": "header",
                "name": "Authorization",
                "description": "F5 XC API Token",
            },
        },
        reconciliation_config={},
        metadata={
            "spec_date": "2024-06-15",
            "download_date": "2024-06-14",
        },
    )


@pytest.fixture
def config_with_renames() -> TransformConfig:
    return TransformConfig(
        input_dir=".",
        output_dir=".",
        transforms={},
        spectral_config={},
        reconciliation_config={
            "schema_renames": [
                {
                    "old_name": "routeRouteType",
                    "new_name": "operateRouteRouteType",
                    "file_pattern": "operate.route",
                },
            ],
            "deprecated_path_removals": [
                {
                    "path": "/api/old",
                    "replacement": "/api/new",
                    "reason": "Superseded",
                },
            ],
        },
        metadata={},
    )


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestTransformerScaffold:
    def test_passthrough_preserves_spec(self, tmp_path, minimal_spec):
        input_dir = tmp_path / "input"
        output_dir = tmp_path / "output"
        input_dir.mkdir()
        output_dir.mkdir()

        spec_file = input_dir / "spec.json"
        spec_file.write_text(json.dumps(minimal_spec))

        config = TransformConfig(
            input_dir=str(input_dir),
            output_dir=str(output_dir),
            transforms={name: False for name, _ in TRANSFORM_REGISTRY},
            spectral_config={},
            reconciliation_config={},
            metadata={},
        )
        transformer = SpecTransformer(config)
        results = transformer.transform_all()

        assert len(results) == 1
        assert results[0].spec["openapi"] == "3.0.0"
        assert results[0].spec["info"]["title"] == "Test"
        assert len(results[0].changes) == 0

    def test_writes_output_files(self, transformer):
        transformer.transform_all()
        saved = transformer.save_results()

        assert len(saved) == 1
        output_path = next(iter(saved.values()))
        assert output_path.exists()
        with output_path.open() as fh:
            written = json.load(fh)
        assert written["openapi"] == "3.0.0"


class TestInjectInfoVersion:
    def test_sets_version_from_spec_date(self, minimal_spec, config_with_metadata):
        result = inject_info_version(minimal_spec, config_with_metadata, "test.json")
        assert result["info"]["version"] == "2024-06-15"

    def test_falls_back_to_download_date(self, minimal_spec):
        config = TransformConfig(
            input_dir=".",
            output_dir=".",
            transforms={},
            spectral_config={},
            reconciliation_config={},
            metadata={"download_date": "2024-06-01"},
        )
        result = inject_info_version(minimal_spec, config, "test.json")
        assert result["info"]["version"] == "2024-06-01"

    def test_idempotent(self, minimal_spec, config_with_metadata):
        result1 = inject_info_version(minimal_spec, config_with_metadata, "test.json")
        result2 = inject_info_version(result1, config_with_metadata, "test.json")
        assert result1["info"]["version"] == result2["info"]["version"]


class TestInjectContact:
    def test_adds_contact(self, minimal_spec, config_with_metadata):
        result = inject_contact(minimal_spec, config_with_metadata, "test.json")
        assert "contact" in result["info"]
        assert result["info"]["contact"]["name"] == "F5 Distributed Cloud"
        assert result["info"]["contact"]["url"] == "https://docs.cloud.f5.com"
        assert result["info"]["contact"]["email"] == "support@f5.com"


class TestInjectServers:
    def test_adds_servers(self, minimal_spec, config_with_metadata):
        result = inject_servers(minimal_spec, config_with_metadata, "test.json")
        assert "servers" in result
        assert len(result["servers"]) == 1
        assert "console.ves.volterra.io" in result["servers"][0]["url"]


class TestInjectSecuritySchemes:
    def test_adds_security_scheme_and_global_security(
        self, minimal_spec, config_with_metadata
    ):
        result = inject_security_schemes(
            minimal_spec, config_with_metadata, "test.json"
        )
        assert "components" in result
        assert "securitySchemes" in result["components"]
        assert "apiKeyAuth" in result["components"]["securitySchemes"]
        scheme = result["components"]["securitySchemes"]["apiKeyAuth"]
        assert scheme["type"] == "apiKey"
        assert scheme["in"] == "header"
        assert scheme["name"] == "Authorization"
        assert "security" in result
        assert result["security"] == [{"apiKeyAuth": []}]


class TestInjectOperationTags:
    def test_tags_all_methods(self, config_with_metadata):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/api/config/namespaces/{namespace}/resources": {
                    "get": {"operationId": "listResources"},
                    "post": {"operationId": "createResource"},
                    "put": {"operationId": "updateResource"},
                    "delete": {"operationId": "deleteResource"},
                }
            },
        }
        result = inject_operation_tags(spec, config_with_metadata, "test.json")
        path_item = result["paths"]["/api/config/namespaces/{namespace}/resources"]
        assert path_item["get"]["tags"] == ["config"]
        assert path_item["post"]["tags"] == ["config"]
        assert path_item["put"]["tags"] == ["config"]
        assert path_item["delete"]["tags"] == ["config"]
        assert any(t["name"] == "config" for t in result.get("tags", []))

    def test_skips_non_parameter_segments(self, config_with_metadata):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/api/{version}/items": {
                    "get": {"operationId": "listItems"},
                }
            },
        }
        result = inject_operation_tags(spec, config_with_metadata, "test.json")
        assert result["paths"]["/api/{version}/items"]["get"]["tags"] == ["items"]

    def test_idempotent(self, config_with_metadata):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/api/config/resources": {"get": {"operationId": "listResources"}},
            },
        }
        result1 = inject_operation_tags(spec, config_with_metadata, "test.json")
        result2 = inject_operation_tags(result1, config_with_metadata, "test.json")
        assert result1["paths"] == result2["paths"]
        assert len(result2.get("tags", [])) == 1


class TestDeduplicateOperationIds:
    def test_appends_method_suffix_to_duplicates(self, config_with_metadata):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/api/resources": {
                    "get": {"operationId": "getResource"},
                    "post": {"operationId": "getResource"},
                },
                "/api/other": {
                    "delete": {"operationId": "getResource"},
                },
            },
        }
        result = deduplicate_operation_ids(spec, config_with_metadata, "test.json")
        get_id = result["paths"]["/api/resources"]["get"]["operationId"]
        post_id = result["paths"]["/api/resources"]["post"]["operationId"]
        del_id = result["paths"]["/api/other"]["delete"]["operationId"]
        assert get_id == "getResource_get"
        assert post_id == "getResource_post"
        assert del_id == "getResource_delete"

    def test_same_method_duplicates_across_paths_get_index_suffix(
        self, config_with_metadata
    ):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/api/path1": {
                    "get": {"operationId": "listThings"},
                },
                "/api/path2": {
                    "get": {"operationId": "listThings"},
                },
            },
        }
        result = deduplicate_operation_ids(spec, config_with_metadata, "test.json")
        id1 = result["paths"]["/api/path1"]["get"]["operationId"]
        id2 = result["paths"]["/api/path2"]["get"]["operationId"]
        assert id1 != id2, f"IDs should differ but both are '{id1}'"

    def test_leaves_unique_ids_unchanged(self, config_with_metadata):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/api/resources": {
                    "get": {"operationId": "listResources"},
                    "post": {"operationId": "createResource"},
                },
            },
        }
        result = deduplicate_operation_ids(spec, config_with_metadata, "test.json")
        assert (
            result["paths"]["/api/resources"]["get"]["operationId"] == "listResources"
        )
        assert (
            result["paths"]["/api/resources"]["post"]["operationId"] == "createResource"
        )


class TestStripScriptTags:
    def test_strips_script_from_description(self, config_with_metadata):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
            "components": {
                "schemas": {
                    "MySchema": {
                        "type": "object",
                        "description": 'Hello <script>alert("xss")</script> World',
                    }
                }
            },
        }
        result = strip_script_tags(spec, config_with_metadata, "test.json")
        desc = result["components"]["schemas"]["MySchema"]["description"]
        assert "<script>" not in desc
        assert "Hello" in desc
        assert "World" in desc

    def test_handles_nested_descriptions(self, config_with_metadata):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/test": {
                    "get": {
                        "description": "<script>bad</script>OK",
                        "responses": {
                            "200": {"description": "Success <script>x</script>"}
                        },
                    }
                }
            },
        }
        result = strip_script_tags(spec, config_with_metadata, "test.json")
        assert "<script>" not in result["paths"]["/test"]["get"]["description"]
        assert (
            "<script>"
            not in result["paths"]["/test"]["get"]["responses"]["200"]["description"]
        )


class TestFixInvalidExamples:
    def test_removes_invalid_default(self, config_with_metadata):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
            "components": {
                "schemas": {
                    "MyEnum": {
                        "type": "string",
                        "enum": ["A", "B", "C"],
                        "default": "INVALID",
                    }
                }
            },
        }
        result = fix_invalid_examples(spec, config_with_metadata, "test.json")
        assert "default" not in result["components"]["schemas"]["MyEnum"]

    def test_keeps_valid_default(self, config_with_metadata):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
            "components": {
                "schemas": {
                    "MyEnum": {
                        "type": "string",
                        "enum": ["A", "B", "C"],
                        "default": "B",
                    }
                }
            },
        }
        result = fix_invalid_examples(spec, config_with_metadata, "test.json")
        assert result["components"]["schemas"]["MyEnum"]["default"] == "B"


class TestRenameCollidingSchemas:
    def test_renames_matching_schema(self, config_with_renames):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
            "components": {
                "schemas": {
                    "routeRouteType": {"type": "string", "enum": ["HTTP", "GRPC"]},
                    "Other": {
                        "type": "object",
                        "properties": {
                            "rt": {"$ref": "#/components/schemas/routeRouteType"},
                        },
                    },
                }
            },
        }
        result = rename_colliding_schemas(
            spec, config_with_renames, "foo.operate.route.json"
        )
        assert "operateRouteRouteType" in result["components"]["schemas"]
        assert "routeRouteType" not in result["components"]["schemas"]
        ref = result["components"]["schemas"]["Other"]["properties"]["rt"]["$ref"]
        assert ref == "#/components/schemas/operateRouteRouteType"

    def test_skips_non_matching_file(self, config_with_renames):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
            "components": {
                "schemas": {
                    "routeRouteType": {"type": "string"},
                }
            },
        }
        result = rename_colliding_schemas(
            spec, config_with_renames, "foo.schema.route.json"
        )
        assert "routeRouteType" in result["components"]["schemas"]
        assert "operateRouteRouteType" not in result["components"]["schemas"]


class TestRemoveDeprecatedPaths:
    def test_removes_configured_path(self, config_with_renames):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/api/old": {"get": {"operationId": "oldEndpoint"}},
                "/api/new": {"get": {"operationId": "newEndpoint"}},
            },
        }
        result = remove_deprecated_paths(spec, config_with_renames, "test.json")
        assert "/api/old" not in result["paths"]
        assert "/api/new" in result["paths"]


class TestMarkDeprecatedOperations:
    def test_marks_deprecated_from_description(self, config_with_metadata):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/api/old": {
                    "get": {
                        "description": "DEPRECATED. Use /api/new instead.",
                        "operationId": "oldGet",
                    }
                },
                "/api/current": {
                    "get": {
                        "description": "Active endpoint.",
                        "operationId": "currentGet",
                    }
                },
            },
        }
        result = mark_deprecated_operations(spec, config_with_metadata, "test.json")
        assert result["paths"]["/api/old"]["get"]["deprecated"] is True
        assert "deprecated" not in result["paths"]["/api/current"]["get"]

    def test_skips_already_deprecated(self, config_with_metadata):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/api/old": {
                    "get": {
                        "description": "DEPRECATED.",
                        "deprecated": True,
                        "operationId": "oldGet",
                    }
                }
            },
        }
        original_state = spec["paths"]["/api/old"]["get"]["deprecated"]
        result = mark_deprecated_operations(spec, config_with_metadata, "test.json")
        assert result["paths"]["/api/old"]["get"]["deprecated"] == original_state


class TestRemoveUnusedSchemas:
    def test_removes_unreferenced_schemas(self, config_with_metadata):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/test": {
                    "get": {
                        "operationId": "getTest",
                        "responses": {
                            "200": {
                                "description": "OK",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/UsedSchema"
                                        }
                                    }
                                },
                            }
                        },
                    }
                }
            },
            "components": {
                "schemas": {
                    "UsedSchema": {"type": "object"},
                    "UnusedSchema": {"type": "string"},
                }
            },
        }
        result = remove_unused_schemas(spec, config_with_metadata, "test.json")
        assert "UsedSchema" in result["components"]["schemas"]
        assert "UnusedSchema" not in result["components"]["schemas"]

    def test_keeps_transitively_referenced_schemas(self, config_with_metadata):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/test": {
                    "get": {
                        "operationId": "getTest",
                        "responses": {
                            "200": {
                                "description": "OK",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/Parent"
                                        }
                                    }
                                },
                            }
                        },
                    }
                }
            },
            "components": {
                "schemas": {
                    "Parent": {
                        "type": "object",
                        "properties": {
                            "child": {"$ref": "#/components/schemas/Child"},
                        },
                    },
                    "Child": {
                        "type": "object",
                        "properties": {
                            "grandchild": {"$ref": "#/components/schemas/Grandchild"},
                        },
                    },
                    "Grandchild": {"type": "string"},
                    "Orphan": {"type": "integer"},
                }
            },
        }
        result = remove_unused_schemas(spec, config_with_metadata, "test.json")
        assert "Parent" in result["components"]["schemas"]
        assert "Child" in result["components"]["schemas"]
        assert "Grandchild" in result["components"]["schemas"]
        assert "Orphan" not in result["components"]["schemas"]

    def test_no_schemas_is_noop(self, minimal_spec, config_with_metadata):
        result = remove_unused_schemas(minimal_spec, config_with_metadata, "test.json")
        assert "components" not in result or "schemas" not in result.get(
            "components", {}
        )


class TestInjectOperationDescriptions:
    def test_generates_description_from_operation_id(self, config_with_metadata):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/api/config/healthchecks": {
                    "get": {"operationId": "ves.io.config.List"},
                }
            },
        }
        result = inject_operation_descriptions(spec, config_with_metadata, "test.json")
        desc = result["paths"]["/api/config/healthchecks"]["get"]["description"]
        assert desc == "List healthchecks."

    def test_skips_existing_descriptions(self, config_with_metadata):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/api/test": {
                    "get": {
                        "operationId": "ves.io.test.Get",
                        "description": "Existing description.",
                    }
                }
            },
        }
        result = inject_operation_descriptions(spec, config_with_metadata, "test.json")
        assert (
            result["paths"]["/api/test"]["get"]["description"]
            == "Existing description."
        )


class TestHTTPMethodsConstant:
    def test_http_methods_includes_all_standard_methods(self):
        expected = {"get", "post", "put", "delete", "patch", "options", "head", "trace"}
        assert expected == HTTP_METHODS
