"""OpenAPI specification loader and parser."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field

import yaml
from openapi_spec_validator import validate
from openapi_spec_validator.versions import consts as openapi_versions
from rich.console import Console

console = Console()


@dataclass
class SchemaInfo:
    """Information about an OpenAPI schema."""

    name: str
    path: str
    schema: dict[str, Any]
    constraints: dict[str, Any] = field(default_factory=dict)

    def get_constraint(self, keyword: str) -> Any:
        """Get a constraint value by keyword."""
        return self.constraints.get(keyword)

    def has_constraint(self, keyword: str) -> bool:
        """Check if schema has a specific constraint."""
        return keyword in self.constraints


@dataclass
class EndpointInfo:
    """Information about an API endpoint."""

    path: str
    method: str
    operation_id: Optional[str]
    request_schema: Optional[SchemaInfo]
    response_schemas: dict[str, SchemaInfo] = field(default_factory=dict)
    parameters: list[dict] = field(default_factory=list)


class SpecLoader:
    """Load and parse OpenAPI specifications."""

    # Constraint keywords to extract from schemas
    CONSTRAINT_KEYWORDS = frozenset([
        # String constraints
        "minLength", "maxLength", "pattern",
        # Numeric constraints
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "multipleOf",
        # Array constraints
        "minItems", "maxItems", "uniqueItems",
        # Object constraints
        "minProperties", "maxProperties", "additionalProperties",
        "propertyNames",
        # Type constraints
        "type", "format", "enum",
        # Composition
        "oneOf", "anyOf", "allOf",
        # Dependencies
        "dependentRequired", "dependentSchemas",
        # Other
        "required", "nullable", "readOnly", "writeOnly",
    ])

    def __init__(self, spec_dir: Path | str):
        self.spec_dir = Path(spec_dir)
        self._specs: dict[str, dict] = {}
        self._schemas: dict[str, SchemaInfo] = {}

    def load_spec(self, filename: str) -> dict[str, Any]:
        """Load a single OpenAPI spec file."""
        filepath = self.spec_dir / filename

        if filename in self._specs:
            return self._specs[filename]

        if not filepath.exists():
            raise FileNotFoundError(f"Spec file not found: {filepath}")

        with open(filepath) as f:
            if filename.endswith(".yaml") or filename.endswith(".yml"):
                spec = yaml.safe_load(f)
            else:
                spec = json.load(f)

        self._specs[filename] = spec
        return spec

    def load_all_domain_files(self) -> dict[str, dict]:
        """Load all domain JSON files from the spec directory."""
        domain_files = {}

        for filepath in self.spec_dir.glob("*.json"):
            try:
                spec = self.load_spec(filepath.name)
                domain_files[filepath.name] = spec
                console.print(f"[green]Loaded: {filepath.name}[/green]")
            except Exception as e:
                console.print(f"[red]Failed to load {filepath.name}: {e}[/red]")

        return domain_files

    def validate_spec(self, spec: dict) -> tuple[bool, list[str]]:
        """Validate an OpenAPI spec and return (is_valid, errors)."""
        errors = []
        try:
            validate(spec)
            return True, []
        except Exception as e:
            errors.append(str(e))
            return False, errors

    def extract_schemas(self, spec: dict) -> dict[str, SchemaInfo]:
        """Extract all schemas from an OpenAPI spec."""
        schemas = {}

        # Get schemas from components
        components = spec.get("components", {})
        for schema_name, schema_def in components.get("schemas", {}).items():
            schema_info = self._parse_schema(schema_name, f"#/components/schemas/{schema_name}", schema_def)
            schemas[schema_name] = schema_info

        return schemas

    def _parse_schema(
        self,
        name: str,
        path: str,
        schema: dict[str, Any],
    ) -> SchemaInfo:
        """Parse a schema definition and extract constraints."""
        constraints = {}

        for keyword in self.CONSTRAINT_KEYWORDS:
            if keyword in schema:
                constraints[keyword] = schema[keyword]

        # Recursively handle nested schemas
        if "properties" in schema:
            for prop_name, prop_schema in schema["properties"].items():
                # Store property constraints
                prop_constraints = {}
                for keyword in self.CONSTRAINT_KEYWORDS:
                    if keyword in prop_schema:
                        prop_constraints[keyword] = prop_schema[keyword]
                if prop_constraints:
                    constraints[f"properties.{prop_name}"] = prop_constraints

        return SchemaInfo(
            name=name,
            path=path,
            schema=schema,
            constraints=constraints,
        )

    def extract_endpoints(self, spec: dict) -> list[EndpointInfo]:
        """Extract all endpoints from an OpenAPI spec."""
        endpoints = []

        for path, path_item in spec.get("paths", {}).items():
            for method in ["get", "post", "put", "patch", "delete"]:
                if method not in path_item:
                    continue

                operation = path_item[method]

                # Extract request schema
                request_schema = None
                if "requestBody" in operation:
                    content = operation["requestBody"].get("content", {})
                    json_content = content.get("application/json", {})
                    if "schema" in json_content:
                        schema_def = json_content["schema"]
                        request_schema = self._parse_schema(
                            f"{method}_{path}_request",
                            f"{path}/{method}/requestBody",
                            schema_def,
                        )

                # Extract response schemas
                response_schemas = {}
                for status_code, response in operation.get("responses", {}).items():
                    content = response.get("content", {})
                    json_content = content.get("application/json", {})
                    if "schema" in json_content:
                        schema_def = json_content["schema"]
                        response_schemas[status_code] = self._parse_schema(
                            f"{method}_{path}_response_{status_code}",
                            f"{path}/{method}/responses/{status_code}",
                            schema_def,
                        )

                # Extract parameters
                parameters = operation.get("parameters", [])
                parameters.extend(path_item.get("parameters", []))

                endpoints.append(EndpointInfo(
                    path=path,
                    method=method.upper(),
                    operation_id=operation.get("operationId"),
                    request_schema=request_schema,
                    response_schemas=response_schemas,
                    parameters=parameters,
                ))

        return endpoints

    def find_schema_by_ref(self, spec: dict, ref: str) -> Optional[dict]:
        """Resolve a $ref to its schema definition."""
        if not ref.startswith("#/"):
            return None

        parts = ref[2:].split("/")
        current = spec

        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None

        return current

    def resolve_refs(self, spec: dict, schema: dict) -> dict:
        """Recursively resolve all $ref in a schema."""
        if "$ref" in schema:
            ref_schema = self.find_schema_by_ref(spec, schema["$ref"])
            if ref_schema:
                return self.resolve_refs(spec, ref_schema)
            return schema

        resolved = {}
        for key, value in schema.items():
            if isinstance(value, dict):
                resolved[key] = self.resolve_refs(spec, value)
            elif isinstance(value, list):
                resolved[key] = [
                    self.resolve_refs(spec, item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                resolved[key] = value

        return resolved

    def get_endpoint_for_resource(
        self,
        spec: dict,
        resource: str,
        operation: str,
    ) -> Optional[EndpointInfo]:
        """Find the endpoint for a specific resource and operation."""
        endpoints = self.extract_endpoints(spec)

        # Map operation names to methods
        operation_method_map = {
            "create": "POST",
            "read": "GET",
            "update": "PUT",
            "delete": "DELETE",
            "list": "GET",
        }

        target_method = operation_method_map.get(operation)

        for endpoint in endpoints:
            if resource in endpoint.path and endpoint.method == target_method:
                # For list vs read, check if path has {name} parameter
                if operation == "list" and "{name}" in endpoint.path:
                    continue
                if operation == "read" and "{name}" not in endpoint.path:
                    continue
                return endpoint

        return None

    def merge_specs(self, specs: list[dict]) -> dict:
        """Merge multiple OpenAPI specs into one."""
        merged = {
            "openapi": "3.0.0",
            "info": {"title": "F5 XC API (Merged)", "version": "1.0.0"},
            "paths": {},
            "components": {"schemas": {}},
        }

        for spec in specs:
            # Merge paths
            merged["paths"].update(spec.get("paths", {}))

            # Merge schemas
            components = spec.get("components", {})
            merged["components"]["schemas"].update(
                components.get("schemas", {})
            )

        return merged


def load_spec_from_file(filepath: Path | str) -> dict:
    """Convenience function to load a single spec file."""
    filepath = Path(filepath)
    with open(filepath) as f:
        if filepath.suffix in (".yaml", ".yml"):
            return yaml.safe_load(f)
        return json.load(f)


def save_spec_to_file(spec: dict, filepath: Path | str, format: str = "json") -> None:
    """Save a spec to file in JSON or YAML format."""
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    with open(filepath, "w") as f:
        if format == "yaml":
            yaml.safe_dump(spec, f, default_flow_style=False, sort_keys=False)
        else:
            json.dump(spec, f, indent=2)
