"""Tests for Spectral linting adapter."""

from __future__ import annotations

import json

from scripts.spectral_lint import SpectralAdapter, map_violation_to_discrepancy
from scripts.utils.constraint_validator import DiscrepancyType

SAMPLE_VIOLATION_SERVERS = {
    "code": "oas3-api-servers",
    "path": [],
    "message": 'OpenAPI "servers" must be present and non-empty array.',
    "severity": 1,
    "range": {
        "start": {"line": 0, "character": 0},
        "end": {"line": 100, "character": 0},
    },
    "source": "/workspace/api-specs/release/specs/test-spec.json",
}

SAMPLE_VIOLATION_TAGS = {
    "code": "operation-tags",
    "path": ["paths", "/api/config/namespaces/{namespace}/resources", "post"],
    "message": 'Operation must have non-empty "tags" array.',
    "severity": 1,
    "range": {
        "start": {"line": 10, "character": 0},
        "end": {"line": 50, "character": 0},
    },
    "source": "/workspace/api-specs/release/specs/test-spec.json",
}

SAMPLE_VIOLATION_INVALID_REF = {
    "code": "invalid-ref",
    "path": ["components", "schemas", "Details", "properties", "status", "$ref"],
    "message": "'#/components/schemas/MissingStatus' does not exist",
    "severity": 0,
    "range": {
        "start": {"line": 300, "character": 0},
        "end": {"line": 302, "character": 0},
    },
    "source": "/workspace/api-specs/release/specs/test-spec.json",
}

SAMPLE_VIOLATION_UNUSED = {
    "code": "oas3-unused-component",
    "path": ["components", "schemas", "UnusedSchema"],
    "message": "Potentially unused component has been detected.",
    "severity": 1,
    "range": {
        "start": {"line": 200, "character": 0},
        "end": {"line": 220, "character": 0},
    },
    "source": "/workspace/api-specs/release/specs/test-spec.json",
}

SAMPLE_VIOLATION_EXAMPLE = {
    "code": "oas3-valid-schema-example",
    "path": ["components", "schemas", "MyEnum", "default"],
    "message": '"default" property must be equal to one of the allowed values.',
    "severity": 0,
    "range": {
        "start": {"line": 300, "character": 0},
        "end": {"line": 300, "character": 20},
    },
    "source": "/workspace/api-specs/release/specs/test-spec.json",
}

SAMPLE_VIOLATION_SCRIPT = {
    "code": "no-script-tags-in-markdown",
    "path": ["components", "schemas", "MySchema", "description"],
    "message": 'Markdown descriptions must not have "<script>" tags.',
    "severity": 1,
    "range": {
        "start": {"line": 400, "character": 0},
        "end": {"line": 400, "character": 100},
    },
    "source": "/workspace/api-specs/release/specs/test-spec.json",
}


class TestMapViolationToDiscrepancy:
    def test_missing_servers_maps_to_spectral_missing(self):
        d = map_violation_to_discrepancy(SAMPLE_VIOLATION_SERVERS)
        assert d.constraint_type == "spectral:oas3-api-servers"
        assert d.discrepancy_type == DiscrepancyType.SPECTRAL_MISSING
        assert d.path == "test-spec.json"

    def test_missing_tags_maps_to_spectral_missing(self):
        d = map_violation_to_discrepancy(SAMPLE_VIOLATION_TAGS)
        assert d.constraint_type == "spectral:operation-tags"
        assert d.discrepancy_type == DiscrepancyType.SPECTRAL_MISSING
        assert d.property_name == "paths./api/config/namespaces/{namespace}/resources.post"

    def test_unused_component_maps_to_spectral_unused(self):
        d = map_violation_to_discrepancy(SAMPLE_VIOLATION_UNUSED)
        assert d.constraint_type == "spectral:oas3-unused-component"
        assert d.discrepancy_type == DiscrepancyType.SPECTRAL_UNUSED
        assert d.property_name == "components.schemas.UnusedSchema"

    def test_invalid_example_maps_to_spectral_invalid(self):
        d = map_violation_to_discrepancy(SAMPLE_VIOLATION_EXAMPLE)
        assert d.constraint_type == "spectral:oas3-valid-schema-example"
        assert d.discrepancy_type == DiscrepancyType.SPECTRAL_INVALID

    def test_script_tags_maps_to_spectral_invalid(self):
        d = map_violation_to_discrepancy(SAMPLE_VIOLATION_SCRIPT)
        assert d.constraint_type == "spectral:no-script-tags-in-markdown"
        assert d.discrepancy_type == DiscrepancyType.SPECTRAL_INVALID


class TestSpectralAdapterGate:
    """The gate must name what failed.

    A bare ``1 errors exceeds max 0`` kept a dangling ``$ref`` unexplained in
    the daily log for thirteen consecutive runs; reading the cause required
    downloading an artifact, so nobody did.
    """

    @staticmethod
    def _capture(capsys) -> str:
        return " ".join(capsys.readouterr().out.split())

    def test_failure_names_every_offending_error(self, monkeypatch, capsys):
        monkeypatch.setenv("COLUMNS", "300")
        adapter = SpectralAdapter(ruleset=".spectral.yaml")

        violations = [SAMPLE_VIOLATION_INVALID_REF, SAMPLE_VIOLATION_TAGS]
        passed = adapter.check_gate(violations, 0, None)

        out = self._capture(capsys)
        assert passed is False
        assert "invalid-ref" in out
        assert "test-spec.json" in out
        assert "components.schemas.Details.properties.status.$ref" in out
        assert "does not exist" in out

    def test_warnings_are_not_listed_when_the_gate_passes(self, monkeypatch, capsys):
        monkeypatch.setenv("COLUMNS", "300")
        adapter = SpectralAdapter(ruleset=".spectral.yaml")

        passed = adapter.check_gate([SAMPLE_VIOLATION_TAGS], 0, None)

        out = self._capture(capsys)
        assert passed is True
        assert "operation-tags" not in out

    def test_warning_budget_breach_names_the_warnings(self, monkeypatch, capsys):
        monkeypatch.setenv("COLUMNS", "300")
        adapter = SpectralAdapter(ruleset=".spectral.yaml")

        passed = adapter.check_gate([SAMPLE_VIOLATION_TAGS], None, 0)

        out = self._capture(capsys)
        assert passed is False
        assert "operation-tags" in out


class TestSpectralAdapterWriteReport:
    def test_write_report_creates_valid_json(self, tmp_path):
        violations = [SAMPLE_VIOLATION_SERVERS, SAMPLE_VIOLATION_TAGS]
        adapter = SpectralAdapter(ruleset=".spectral.yaml")
        report_path = tmp_path / "spectral_report.json"
        adapter.write_report(violations, report_path)

        report = json.loads(report_path.read_text())
        assert "discrepancies" in report
        assert len(report["discrepancies"]) == 2
        assert report["discrepancies"][0]["constraint_type"] == "spectral:oas3-api-servers"
