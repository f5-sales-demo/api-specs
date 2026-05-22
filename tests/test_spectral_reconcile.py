"""Tests for transforms migrated from the spectral reconcile module.

These tests verify that the transform functions produce the same results
as the original reconcile Spectral fixers.
"""

from __future__ import annotations

import pytest

from scripts.transform import (
    TransformConfig,
    deduplicate_operation_ids,
    fix_invalid_examples,
    inject_contact,
    inject_security_schemes,
    inject_servers,
    mark_deprecated_operations,
    remove_deprecated_paths,
    remove_unused_schemas,
    rename_colliding_schemas,
    strip_script_tags,
)


@pytest.fixture
def spectral_config():
    return TransformConfig(
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
                }
            ],
            "security_scheme": {
                "type": "apiKey",
                "in": "header",
                "name": "Authorization",
                "description": "F5 XC API Token",
            },
        },
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
    )


class TestAddServers:
    def test_adds_servers_to_spec(self, spectral_config):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
        }
        result = inject_servers(spec, spectral_config, "test.json")
        assert "servers" in result
        assert len(result["servers"]) == 1
        assert "console.ves.volterra.io" in result["servers"][0]["url"]


class TestAddContact:
    def test_adds_contact_to_info(self, spectral_config):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
        }
        result = inject_contact(spec, spectral_config, "test.json")
        assert "contact" in result["info"]
        assert result["info"]["contact"]["name"] == "F5 Distributed Cloud"


class TestRemoveUnusedComponent:
    def test_removes_unused_schema(self, spectral_config):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
            "components": {
                "schemas": {
                    "UsedSchema": {"type": "object"},
                    "UnusedSchema": {
                        "type": "object",
                        "properties": {"x": {"type": "string"}},
                    },
                }
            },
        }
        result = remove_unused_schemas(spec, spectral_config, "test.json")
        assert "UnusedSchema" not in result["components"]["schemas"]


class TestFixSchemaExample:
    def test_removes_invalid_default(self, spectral_config):
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
        result = fix_invalid_examples(spec, spectral_config, "test.json")
        assert "default" not in result["components"]["schemas"]["MyEnum"]


class TestStripScriptTags:
    def test_strips_script_from_description(self, spectral_config):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
            "components": {
                "schemas": {
                    "MySchema": {
                        "type": "object",
                        "description": 'Text <script>alert("xss")</script> more',
                    }
                }
            },
        }
        result = strip_script_tags(spec, spectral_config, "test.json")
        desc = result["components"]["schemas"]["MySchema"]["description"]
        assert "<script>" not in desc
        assert "Text" in desc


class TestDeduplicateOperationId:
    def test_appends_method_suffix(self, spectral_config):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/api/resources": {
                    "get": {
                        "operationId": "getResource",
                        "responses": {"200": {"description": "OK"}},
                    },
                    "post": {
                        "operationId": "getResource",
                        "responses": {"201": {"description": "Created"}},
                    },
                }
            },
        }
        result = deduplicate_operation_ids(spec, spectral_config, "test.json")
        post_id = result["paths"]["/api/resources"]["post"]["operationId"]
        get_id = result["paths"]["/api/resources"]["get"]["operationId"]
        assert post_id != get_id


class TestAddSecuritySchemes:
    def test_adds_security_metadata(self, spectral_config):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
        }
        result = inject_security_schemes(spec, spectral_config, "test.json")
        assert "security" in result
        assert "securitySchemes" in result["components"]
        assert "apiKeyAuth" in result["components"]["securitySchemes"]
        assert result["components"]["securitySchemes"]["apiKeyAuth"]["type"] == "apiKey"


class TestSchemaRename:
    def test_rename_applied_to_matching_file(self, spectral_config):
        spec = {
            "components": {
                "schemas": {
                    "routeRouteType": {"type": "string", "enum": ["A"]},
                    "other": {
                        "properties": {
                            "rt": {"$ref": "#/components/schemas/routeRouteType"}
                        }
                    },
                },
            },
        }
        result = rename_colliding_schemas(
            spec, spectral_config, "foo.operate.route.json"
        )
        assert "operateRouteRouteType" in result["components"]["schemas"]
        assert "routeRouteType" not in result["components"]["schemas"]

    def test_rename_skipped_for_non_matching_file(self, spectral_config):
        spec = {"components": {"schemas": {"routeRouteType": {"type": "object"}}}}
        result = rename_colliding_schemas(
            spec, spectral_config, "foo.schema.route.json"
        )
        assert "routeRouteType" in result["components"]["schemas"]


class TestDeprecatedMarkers:
    def test_marks_deprecated_from_description(self, spectral_config):
        spec = {
            "paths": {
                "/api/old": {
                    "get": {
                        "description": "DEPRECATED. Use /api/new.",
                        "responses": {"200": {"description": "OK"}},
                    },
                },
                "/api/current": {
                    "get": {
                        "description": "Active.",
                        "responses": {"200": {"description": "OK"}},
                    },
                },
            },
        }
        result = mark_deprecated_operations(spec, spectral_config, "test.json")
        assert result["paths"]["/api/old"]["get"]["deprecated"] is True
        assert "deprecated" not in result["paths"]["/api/current"]["get"]

    def test_removes_deprecated_path_from_config(self, spectral_config):
        spec = {
            "paths": {
                "/api/old": {"get": {"description": "Old"}},
                "/api/new": {"get": {"description": "New"}},
            },
        }
        result = remove_deprecated_paths(spec, spectral_config, "test.json")
        assert "/api/old" not in result["paths"]
        assert "/api/new" in result["paths"]
