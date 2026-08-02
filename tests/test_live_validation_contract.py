"""Fail-closed contracts for authenticated API specification validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from openapi_spec_validator import validate

from scripts.utils.schemathesis_runner import SchemathesisResult
from scripts.utils.schemathesis_runner import TestStatus as _TestStatus
from scripts.validate import (
    LiveValidationError,
    ValidationTarget,
    resolve_validation_targets,
    validate_live_results,
)

_TARGET = ValidationTarget(
    endpoint_name="healthcheck",
    domain="healthcheck",
    filename="healthcheck.json",
    operations=(("GET", "/healthchecks"),),
)


def _spec(*operations: tuple[str, str]) -> dict:
    paths: dict[str, dict] = {}
    for method, path in operations:
        paths.setdefault(path, {})[method.lower()] = {"responses": {"200": {"description": "OK"}}}
    return {"openapi": "3.0.0", "info": {"title": "test", "version": "1"}, "paths": paths}


def _endpoints(domain: str = "healthcheck") -> dict:
    return {
        "endpoints": {
            "healthcheck": {
                "domain": domain,
                "operations": {
                    "list": "GET /api/config/namespaces/{namespace}/healthchecks",
                    "read": "GET /api/config/namespaces/{namespace}/healthchecks/{name}",
                },
            }
        },
        "test_order": ["healthcheck"],
    }


def test_semantic_domain_resolution_survives_numbered_filename_churn() -> None:
    filename = "docs-cloud-f5-com.9876.public.ves.io.schema.healthcheck.ves-swagger.json"
    specs = {
        filename: _spec(
            ("GET", "/api/config/namespaces/{namespace}/healthchecks"),
            ("GET", "/api/config/namespaces/{namespace}/healthchecks/{name}"),
        )
    }

    targets = resolve_validation_targets(specs, _endpoints())

    assert len(targets) == 1
    assert targets[0].domain == "healthcheck"
    assert targets[0].filename == filename
    assert targets[0].operations == (
        ("GET", "/api/config/namespaces/{namespace}/healthchecks"),
        ("GET", "/api/config/namespaces/{namespace}/healthchecks/{name}"),
    )


@pytest.mark.parametrize(
    ("specs", "config", "message"),
    [
        ({}, _endpoints(), "domain 'healthcheck' resolved to 0 files"),
        (
            {
                "docs-cloud-f5-com.0001.public.ves.io.schema.healthcheck.ves-swagger.json": _spec(),
                "docs-cloud-f5-com.0002.public.ves.io.schema.healthcheck.ves-swagger.json": _spec(),
            },
            _endpoints(),
            "domain 'healthcheck' resolved to 2 files",
        ),
        (
            {"docs-cloud-f5-com.0001.public.ves.io.schema.healthcheck.ves-swagger.json": _spec()},
            {
                "endpoints": {
                    "healthcheck": {
                        "domain_file": "numbered-file.json",
                        "operations": {"list": "GET /healthchecks"},
                    }
                },
                "test_order": ["healthcheck"],
            },
            "must declare semantic domain",
        ),
    ],
)
def test_domain_resolution_fails_closed(specs: dict, config: dict, message: str) -> None:
    with pytest.raises(LiveValidationError, match=message):
        resolve_validation_targets(specs, config)


def test_configured_operation_must_exist_in_resolved_spec() -> None:
    filename = "docs-cloud-f5-com.0001.public.ves.io.schema.healthcheck.ves-swagger.json"
    specs = {filename: _spec(("GET", "/api/config/namespaces/{namespace}/healthchecks"))}

    with pytest.raises(LiveValidationError, match="configured operation does not exist"):
        resolve_validation_targets(specs, _endpoints())


def test_test_order_must_name_every_endpoint_exactly_once() -> None:
    filename = "docs-cloud-f5-com.0001.public.ves.io.schema.healthcheck.ves-swagger.json"
    specs = {
        filename: _spec(
            ("GET", "/api/config/namespaces/{namespace}/healthchecks"),
            ("GET", "/api/config/namespaces/{namespace}/healthchecks/{name}"),
        )
    }
    config = _endpoints()
    config["test_order"] = []

    with pytest.raises(LiveValidationError, match="test_order must name every configured endpoint"):
        resolve_validation_targets(specs, config)


@pytest.mark.parametrize(
    ("results", "message"),
    [
        ([], "executed 0 of 1 configured operations"),
        (
            [SchemathesisResult("/healthchecks", "GET", _TestStatus.PASSED, examples_tested=0)],
            "executed zero examples",
        ),
        (
            [
                SchemathesisResult(
                    "/healthchecks",
                    "GET",
                    _TestStatus.ERROR,
                    examples_tested=1,
                    errors=[{"error": "request failed"}],
                )
            ],
            "finished with error status",
        ),
    ],
)
def test_live_result_contract_rejects_vacuous_or_error_runs(
    results: list[SchemathesisResult], message: str
) -> None:
    with pytest.raises(LiveValidationError, match=message):
        validate_live_results(
            _TARGET,
            (("GET", "/healthchecks"),),
            results,
        )


def test_live_result_contract_accepts_every_configured_operation_with_evidence() -> None:
    results = [SchemathesisResult("/healthchecks", "GET", _TestStatus.PASSED, examples_tested=3)]

    validate_live_results(
        _TARGET,
        (("GET", "/healthchecks"),),
        results,
    )


def test_repository_validation_config_resolves_against_release_specs() -> None:
    root = Path(__file__).parents[1]
    specs = {
        path.name: json.loads(path.read_text())
        for path in (root / "release" / "specs").glob("*.json")
        if path.name != ".spec_metadata.json"
    }
    endpoints = yaml.safe_load((root / "config" / "endpoints.yaml").read_text())

    targets = resolve_validation_targets(specs, endpoints)

    assert len(targets) == len(endpoints["endpoints"])
    assert sum(len(target.operations) for target in targets) == 10


def test_committed_corrected_intermediate_specs_are_all_valid_openapi() -> None:
    root = Path(__file__).parents[1]
    failures: list[str] = []
    paths = [
        path
        for path in (root / "release" / "specs").glob("*.json")
        if not path.name.startswith(".")
    ]

    for path in paths:
        try:
            validate(json.loads(path.read_text()))
        except Exception as error:  # pylint: disable=broad-exception-caught
            failures.append(f"{path.name}: {error}")

    assert len(paths) > 200
    assert not failures, "corrected intermediate contains invalid specs:\n" + "\n".join(failures)
