"""Tests for Spectral-specific reconciliation fixes."""

# pylint: disable=protected-access

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.reconcile import ReconciliationConfig, SpecReconciler
from scripts.utils.constraint_validator import Discrepancy, DiscrepancyType


@pytest.fixture
def spec_without_servers():
    return {
        "openapi": "3.0.0",
        "info": {"title": "Test", "version": "1.0.0"},
        "paths": {},
    }


@pytest.fixture
def spec_without_contact():
    return {
        "openapi": "3.0.0",
        "info": {"title": "Test", "version": "1.0.0"},
        "paths": {},
    }


@pytest.fixture
def spec_without_tags():
    return {
        "openapi": "3.0.0",
        "info": {"title": "Test", "version": "1.0.0"},
        "paths": {
            "/api/config/namespaces/{namespace}/resources": {
                "get": {
                    "operationId": "listResources",
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }


@pytest.fixture
def spec_with_unused_component():
    return {
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


@pytest.fixture
def spec_with_bad_default():
    return {
        "openapi": "3.0.0",
        "info": {"title": "Test", "version": "1.0.0"},
        "paths": {},
        "components": {
            "schemas": {
                "MyEnum": {
                    "type": "string",
                    "enum": ["A", "B", "C"],
                    "default": "INVALID_VALUE",
                }
            }
        },
    }


@pytest.fixture
def spec_with_script_tags():
    return {
        "openapi": "3.0.0",
        "info": {"title": "Test", "version": "1.0.0"},
        "paths": {},
        "components": {
            "schemas": {
                "MySchema": {
                    "type": "object",
                    "description": 'Some text <script>alert("xss")</script> more text',
                }
            }
        },
    }


@pytest.fixture
def spec_with_duplicate_ids():
    return {
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


@pytest.fixture
def reconciler(tmp_path):
    original_dir = tmp_path / "original"
    output_dir = tmp_path / "output"
    original_dir.mkdir()
    output_dir.mkdir()

    spectral_config = {
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
    }

    return SpecReconciler(
        original_dir=original_dir,
        output_dir=output_dir,
        config=ReconciliationConfig(),
        spectral_config=spectral_config,
    )


class TestAddServers:
    def test_adds_servers_to_spec(self, reconciler, spec_without_servers):
        d = Discrepancy(
            path="test.json",
            property_name="",
            constraint_type="spectral:oas3-api-servers",
            discrepancy_type=DiscrepancyType.SPECTRAL_MISSING,
            spec_value=None,
            api_behavior=None,
        )
        result = reconciler._add_servers(spec_without_servers, d)
        assert result is not None
        assert "servers" in result
        assert len(result["servers"]) == 1
        assert "console.ves.volterra.io" in result["servers"][0]["url"]


class TestAddContact:
    def test_adds_contact_to_info(self, reconciler, spec_without_contact):
        d = Discrepancy(
            path="test.json",
            property_name="info",
            constraint_type="spectral:info-contact",
            discrepancy_type=DiscrepancyType.SPECTRAL_MISSING,
            spec_value=None,
            api_behavior=None,
        )
        result = reconciler._add_contact(spec_without_contact, d)
        assert result is not None
        assert "contact" in result["info"]
        assert result["info"]["contact"]["name"] == "F5 Distributed Cloud"


class TestAddTags:
    def test_derives_tag_from_path(self, reconciler, spec_without_tags):
        d = Discrepancy(
            path="test.json",
            property_name="paths./api/config/namespaces/{namespace}/resources.get",
            constraint_type="spectral:operation-tags",
            discrepancy_type=DiscrepancyType.SPECTRAL_MISSING,
            spec_value=None,
            api_behavior=None,
        )
        result = reconciler._add_tags(spec_without_tags, d)
        assert result is not None
        op = result["paths"]["/api/config/namespaces/{namespace}/resources"]["get"]
        assert "tags" in op
        assert "config" in op["tags"]
        assert any(t["name"] == "config" for t in result.get("tags", []))


class TestRemoveUnusedComponent:
    def test_removes_unused_schema(self, reconciler, spec_with_unused_component):
        d = Discrepancy(
            path="test.json",
            property_name="components.schemas.UnusedSchema",
            constraint_type="spectral:oas3-unused-component",
            discrepancy_type=DiscrepancyType.SPECTRAL_UNUSED,
            spec_value=None,
            api_behavior=None,
        )
        result = reconciler._remove_unused_component(spec_with_unused_component, d)
        assert result is not None
        assert "UnusedSchema" not in result["components"]["schemas"]
        assert "UsedSchema" in result["components"]["schemas"]


class TestFixSchemaExample:
    def test_removes_invalid_default(self, reconciler, spec_with_bad_default):
        d = Discrepancy(
            path="test.json",
            property_name="components.schemas.MyEnum.default",
            constraint_type="spectral:oas3-valid-schema-example",
            discrepancy_type=DiscrepancyType.SPECTRAL_INVALID,
            spec_value=None,
            api_behavior=None,
        )
        result = reconciler._fix_schema_example(spec_with_bad_default, d)
        assert result is not None
        assert "default" not in result["components"]["schemas"]["MyEnum"]


class TestStripScriptTags:
    def test_strips_script_from_description(self, reconciler, spec_with_script_tags):
        d = Discrepancy(
            path="test.json",
            property_name="components.schemas.MySchema.description",
            constraint_type="spectral:no-script-tags-in-markdown",
            discrepancy_type=DiscrepancyType.SPECTRAL_INVALID,
            spec_value=None,
            api_behavior=None,
        )
        result = reconciler._strip_script_tags(spec_with_script_tags, d)
        assert result is not None
        desc = result["components"]["schemas"]["MySchema"]["description"]
        assert "<script>" not in desc
        assert "Some text" in desc
        assert "more text" in desc


class TestDeduplicateOperationId:
    def test_appends_method_suffix(self, reconciler, spec_with_duplicate_ids):
        d = Discrepancy(
            path="test.json",
            property_name="paths./api/resources.post.operationId",
            constraint_type="spectral:operation-operationId-unique",
            discrepancy_type=DiscrepancyType.SPECTRAL_INVALID,
            spec_value=None,
            api_behavior=None,
        )
        result = reconciler._deduplicate_operation_id(spec_with_duplicate_ids, d)
        assert result is not None
        post_id = result["paths"]["/api/resources"]["post"]["operationId"]
        get_id = result["paths"]["/api/resources"]["get"]["operationId"]
        assert post_id != get_id


class TestAddSecuritySchemes:
    def test_adds_security_metadata(self, reconciler):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
        }
        # Override spectral_config for test
        reconciler.spectral_config["security_scheme"] = {
            "type": "apiKey",
            "in": "header",
            "name": "Authorization",
            "description": "F5 XC API Token",
        }
        d = Discrepancy(
            path="test.json",
            property_name="",
            constraint_type="spectral:checkov-security",
            discrepancy_type=DiscrepancyType.SPECTRAL_MISSING,
            spec_value=None,
            api_behavior=None,
        )
        result = reconciler._add_security_schemes(spec, d)
        assert result is not None
        assert "security" in result
        assert "securitySchemes" in result["components"]
        assert "apiKeyAuth" in result["components"]["securitySchemes"]
        scheme = result["components"]["securitySchemes"]["apiKeyAuth"]
        assert scheme["type"] == "apiKey"
        assert scheme["name"] == "Authorization"


class TestSchemaRename:
    """Tests for config-driven schema rename functionality."""

    @staticmethod
    def _make_reconciler(renames):
        config = ReconciliationConfig(schema_renames=renames)
        return SpecReconciler(
            original_dir=Path(),
            output_dir=Path(),
            config=config,
        )

    def test_rename_applied_to_matching_file(self):
        reconciler = self._make_reconciler(
            [
                {
                    "old_name": "routeRouteType",
                    "new_name": "operateRouteRouteType",
                    "file_pattern": "operate.route",
                },
            ]
        )
        spec: dict = {
            "components": {
                "schemas": {
                    "routeRouteType": {"type": "string", "enum": ["A"]},
                    "other": {
                        "properties": {
                            "rt": {"$ref": "#/components/schemas/routeRouteType"},
                        },
                    },
                },
            },
        }
        changes = reconciler._apply_schema_renames(spec, "foo.operate.route.json")
        assert len(changes) == 1
        assert "operateRouteRouteType" in spec["components"]["schemas"]
        assert "routeRouteType" not in spec["components"]["schemas"]
        other = spec["components"]["schemas"]["other"]
        assert isinstance(other, dict)
        props = other["properties"]
        assert isinstance(props, dict)
        assert props["rt"]["$ref"] == "#/components/schemas/operateRouteRouteType"

    def test_rename_skipped_for_non_matching_file(self):
        reconciler = self._make_reconciler(
            [
                {
                    "old_name": "routeRouteType",
                    "new_name": "operateRouteRouteType",
                    "file_pattern": "operate.route",
                },
            ]
        )
        spec: dict = {
            "components": {
                "schemas": {
                    "routeRouteType": {"type": "object", "properties": {}},
                },
            },
        }
        changes = reconciler._apply_schema_renames(spec, "foo.schema.route.json")
        assert len(changes) == 0
        assert "routeRouteType" in spec["components"]["schemas"]

    def test_rename_skipped_when_schema_absent(self):
        reconciler = self._make_reconciler(
            [
                {
                    "old_name": "routeRouteType",
                    "new_name": "operateRouteRouteType",
                    "file_pattern": "operate.route",
                },
            ]
        )
        spec: dict = {"components": {"schemas": {"other": {"type": "object"}}}}
        changes = reconciler._apply_schema_renames(spec, "foo.operate.route.json")
        assert len(changes) == 0

    def test_no_renames_when_config_empty(self):
        reconciler = self._make_reconciler([])
        spec: dict = {
            "components": {"schemas": {"routeRouteType": {"type": "string"}}},
        }
        changes = reconciler._apply_schema_renames(spec, "anything.json")
        assert len(changes) == 0
