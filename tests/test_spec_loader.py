"""Tests for spec loader module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.utils.spec_loader import SpecLoader, SchemaInfo, EndpointInfo


class TestSpecLoader:
    """Tests for SpecLoader class."""

    @pytest.fixture
    def loader(self, temp_dir: Path, sample_openapi_spec: dict) -> SpecLoader:
        """Create a spec loader with sample spec."""
        # Write sample spec to temp dir
        spec_path = temp_dir / "test.json"
        with open(spec_path, "w") as f:
            json.dump(sample_openapi_spec, f)

        return SpecLoader(temp_dir)

    def test_load_spec(self, loader: SpecLoader):
        """Test loading a spec file."""
        spec = loader.load_spec("test.json")

        assert spec is not None
        assert spec["openapi"] == "3.0.0"
        assert "paths" in spec
        assert "components" in spec

    def test_load_spec_caching(self, loader: SpecLoader):
        """Test that specs are cached after first load."""
        spec1 = loader.load_spec("test.json")
        spec2 = loader.load_spec("test.json")

        assert spec1 is spec2  # Same object reference

    def test_load_spec_not_found(self, loader: SpecLoader):
        """Test loading non-existent spec."""
        with pytest.raises(FileNotFoundError):
            loader.load_spec("nonexistent.json")

    def test_validate_spec_valid(self, loader: SpecLoader, sample_openapi_spec: dict):
        """Test validating a valid spec."""
        is_valid, errors = loader.validate_spec(sample_openapi_spec)

        assert is_valid is True
        assert len(errors) == 0

    def test_validate_spec_invalid(self, loader: SpecLoader):
        """Test validating an invalid spec."""
        invalid_spec = {"invalid": "spec"}
        is_valid, errors = loader.validate_spec(invalid_spec)

        assert is_valid is False
        assert len(errors) > 0

    def test_extract_schemas(self, loader: SpecLoader, sample_openapi_spec: dict):
        """Test extracting schemas from spec."""
        schemas = loader.extract_schemas(sample_openapi_spec)

        assert "TestRequest" in schemas
        assert "TestResponse" in schemas

        request_schema = schemas["TestRequest"]
        assert isinstance(request_schema, SchemaInfo)
        assert request_schema.name == "TestRequest"

    def test_schema_constraints(self, loader: SpecLoader, sample_openapi_spec: dict):
        """Test that constraints are extracted from schemas."""
        schemas = loader.extract_schemas(sample_openapi_spec)

        request_schema = schemas["TestRequest"]
        constraints = request_schema.constraints

        assert "required" in constraints
        assert "name" in constraints["required"]

    def test_extract_endpoints(self, loader: SpecLoader, sample_openapi_spec: dict):
        """Test extracting endpoints from spec."""
        endpoints = loader.extract_endpoints(sample_openapi_spec)

        assert len(endpoints) == 2  # GET and POST

        get_endpoint = next(e for e in endpoints if e.method == "GET")
        assert get_endpoint.path == "/test"
        assert get_endpoint.operation_id == "getTest"

        post_endpoint = next(e for e in endpoints if e.method == "POST")
        assert post_endpoint.path == "/test"
        assert post_endpoint.operation_id == "createTest"
        assert post_endpoint.request_schema is not None

    def test_find_schema_by_ref(self, loader: SpecLoader, sample_openapi_spec: dict):
        """Test finding schema by $ref."""
        schema = loader.find_schema_by_ref(
            sample_openapi_spec,
            "#/components/schemas/TestRequest"
        )

        assert schema is not None
        assert schema["type"] == "object"
        assert "properties" in schema

    def test_find_schema_by_ref_not_found(
        self, loader: SpecLoader, sample_openapi_spec: dict
    ):
        """Test finding non-existent schema by $ref."""
        schema = loader.find_schema_by_ref(
            sample_openapi_spec,
            "#/components/schemas/NonExistent"
        )

        assert schema is None

    def test_resolve_refs(self, loader: SpecLoader, sample_openapi_spec: dict):
        """Test resolving $ref in schema."""
        schema_with_ref = {"$ref": "#/components/schemas/TestRequest"}
        resolved = loader.resolve_refs(sample_openapi_spec, schema_with_ref)

        assert "$ref" not in resolved
        assert resolved["type"] == "object"


class TestSchemaInfo:
    """Tests for SchemaInfo dataclass."""

    def test_schema_info_creation(self):
        """Test creating SchemaInfo."""
        schema = SchemaInfo(
            name="TestSchema",
            path="#/components/schemas/TestSchema",
            schema={"type": "object"},
            constraints={"minLength": 1},
        )

        assert schema.name == "TestSchema"
        assert schema.path == "#/components/schemas/TestSchema"
        assert schema.schema["type"] == "object"

    def test_get_constraint(self):
        """Test getting constraint value."""
        schema = SchemaInfo(
            name="Test",
            path="",
            schema={},
            constraints={"minLength": 5, "maxLength": 100},
        )

        assert schema.get_constraint("minLength") == 5
        assert schema.get_constraint("maxLength") == 100
        assert schema.get_constraint("pattern") is None

    def test_has_constraint(self):
        """Test checking for constraint presence."""
        schema = SchemaInfo(
            name="Test",
            path="",
            schema={},
            constraints={"minLength": 5},
        )

        assert schema.has_constraint("minLength") is True
        assert schema.has_constraint("pattern") is False


class TestEndpointInfo:
    """Tests for EndpointInfo dataclass."""

    def test_endpoint_info_creation(self):
        """Test creating EndpointInfo."""
        endpoint = EndpointInfo(
            path="/api/test",
            method="POST",
            operation_id="createTest",
            request_schema=None,
        )

        assert endpoint.path == "/api/test"
        assert endpoint.method == "POST"
        assert endpoint.operation_id == "createTest"
        assert endpoint.request_schema is None
        assert endpoint.response_schemas == {}
        assert endpoint.parameters == []
