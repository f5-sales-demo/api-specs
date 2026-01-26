"""Pytest configuration and fixtures."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def temp_dir() -> Generator[Path]:
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_openapi_spec() -> dict:
    """Return a minimal valid OpenAPI 3.0 spec."""
    return {
        "openapi": "3.0.0",
        "info": {
            "title": "Test API",
            "version": "1.0.0",
        },
        "paths": {
            "/test": {
                "get": {
                    "operationId": "getTest",
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/TestResponse"}
                                }
                            },
                        }
                    },
                },
                "post": {
                    "operationId": "createTest",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/TestRequest"}
                            }
                        }
                    },
                    "responses": {"201": {"description": "Created"}},
                },
            }
        },
        "components": {
            "schemas": {
                "TestRequest": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 100,
                            "pattern": "^[a-z][a-z0-9-]*$",
                        },
                        "count": {"type": "integer", "minimum": 0, "maximum": 1000},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 0,
                            "maxItems": 10,
                            "uniqueItems": True,
                        },
                        "status": {"type": "string", "enum": ["active", "inactive", "pending"]},
                    },
                },
                "TestResponse": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "created_at": {"type": "string", "format": "date-time"},
                    },
                },
            }
        },
    }


@pytest.fixture
def sample_spec_file(temp_dir: Path, sample_openapi_spec: dict) -> Path:
    """Create a sample spec file."""
    spec_path = temp_dir / "test_spec.json"
    with open(spec_path, "w") as f:
        json.dump(sample_openapi_spec, f)
    return spec_path


@pytest.fixture
def sample_config() -> dict:
    """Return sample validation configuration."""
    return {
        "api": {
            "base_url": "https://test.example.com",
            "tenant": "example-tenant",
            "namespace": "example-namespace",
            "timeout": 30,
            "retries": 3,
        },
        "download": {
            "url": "https://example.com/specs.zip",
            "output_dir": "specs/original",
            "etag_cache": ".etag_cache",
        },
        "validation_categories": {
            "string_length": {"enabled": True},
            "pattern": {"enabled": True},
            "numeric_bounds": {"enabled": True},
            "required_fields": {"enabled": True},
            "enum_values": {"enabled": True},
        },
        "schemathesis": {
            "enabled": True,
            "max_examples": 10,
        },
        "reports": {
            "output_dir": "reports",
            "formats": ["json", "markdown"],
        },
    }


@pytest.fixture
def sample_config_file(temp_dir: Path, sample_config: dict) -> Path:
    """Create a sample config file."""
    config_path = temp_dir / "validation.yaml"
    with open(config_path, "w") as f:
        yaml.safe_dump(sample_config, f)
    return config_path


@pytest.fixture
def sample_endpoints_config() -> dict:
    """Return sample endpoints configuration."""
    return {
        "endpoints": {
            "healthcheck": {
                "resource": "healthchecks",
                "domain_file": "virtual.json",
                "api_group": "config",
                "crud_operations": {
                    "create": "POST /api/config/namespaces/{namespace}/healthchecks",
                    "read": "GET /api/config/namespaces/{namespace}/healthchecks/{name}",
                    "list": "GET /api/config/namespaces/{namespace}/healthchecks",
                    "update": "PUT /api/config/namespaces/{namespace}/healthchecks/{name}",
                    "delete": "DELETE /api/config/namespaces/{namespace}/healthchecks/{name}",
                },
            }
        },
        "test_order": ["healthcheck"],
    }
