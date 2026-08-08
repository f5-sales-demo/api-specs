"""Tests for the reconcile.py CLI behavior with strict outcomes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.reconcile import load_discrepancies
from scripts.reconcile import main as reconcile_main
from scripts.utils.reconciliation_report import (
    validate_reconciliation_report,
)


def _make_discrepancy(**kwargs: Any) -> dict[str, Any]:
    """Helper to create a fully populated version-1 schema discrepancy dict."""
    default = {
        "path": "TestModel",
        "property_name": "TestModel",
        "constraint_type": "maxLength",
        "discrepancy_type": "spec_stricter",
        "spec_file": "test_spec.json",
        "domain": "test_domain",
        "method": "POST",
        "spec_value": 10,
        "api_behavior": 20,
        "test_values": [],
        "recommendation": "recommendation",
    }
    default.update(kwargs)
    return default


@pytest.fixture
def setup_reconcile_env(monkeypatch, tmp_path):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()

    original_dir = tmp_path / "original"
    original_dir.mkdir()

    output_dir = tmp_path / "release_specs"
    output_dir.mkdir()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    schema_src = Path(__file__).parent.parent / "config" / "reconciliation_report.schema.json"
    (config_dir / "reconciliation_report.schema.json").write_text(schema_src.read_text())

    monkeypatch.chdir(tmp_path)

    def _run(
        discrepancies: list | dict | None,
        specs: dict[str, dict],
        report_out: str | None = None,
        simulate_missing_report: bool = False,
    ) -> int:
        val_report = reports_dir / "validation_report.json"

        if not simulate_missing_report:
            if discrepancies is not None:
                if isinstance(discrepancies, list):
                    val_report.write_text(json.dumps({"discrepancies": discrepancies}))
                else:
                    val_report.write_text(json.dumps(discrepancies))
            else:
                val_report.write_text(json.dumps({"discrepancies": []}))

        for name, content in specs.items():
            (original_dir / name).write_text(json.dumps(content))

        args = [
            "reconcile.py",
            "--original-dir",
            str(original_dir),
            "--output-dir",
            str(output_dir),
        ]
        if not simulate_missing_report:
            args.extend(["--report", str(val_report)])
        else:
            args.extend(["--report", str(reports_dir / "nonexistent_report.json")])

        if report_out:
            args.extend(["--reconciliation-report-out", report_out])

        monkeypatch.setattr("sys.argv", args)
        return reconcile_main()

    return _run, reports_dir, output_dir, config_dir


def test_reconcile_zero_fixes(setup_reconcile_env):
    """Zero discrepancies return zero with a valid empty report."""
    run, reports, out, config = setup_reconcile_env
    specs = {
        "test_spec.json": {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
        }
    }
    ret = run([], specs)
    assert ret == 0

    report_file = Path("reports/reconciliation_report.json")
    assert report_file.exists()

    data = json.loads(report_file.read_text())
    validate_reconciliation_report(data)

    assert data["summary"]["processed_specs"] == 1
    assert data["summary"]["fixes_applied"] == 0
    assert data["fixes"] == []
    assert data["failures"] == []


def test_reconcile_successful_fix(setup_reconcile_env):
    """An all-success reconciliation returns zero with correct outcome mapping and provenance."""
    run, reports, out, config = setup_reconcile_env
    specs = {
        "test_spec.json": {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
            "components": {"schemas": {"TestModel": {"type": "string", "maxLength": 10}}},
        }
    }

    discrepancies = [
        _make_discrepancy(
            spec_file="test_spec.json",
            path="TestModel",
            property_name="TestModel",
            constraint_type="maxLength",
            discrepancy_type="spec_stricter",
            spec_value=10,
            api_behavior=20,
            domain="test_domain",
            method="POST",
        )
    ]

    ret = run(discrepancies, specs)
    assert ret == 0

    report_file = Path("reports/reconciliation_report.json")
    assert report_file.exists()

    data = json.loads(report_file.read_text())
    validate_reconciliation_report(data)

    assert data["summary"]["fixes_applied"] == 1
    assert data["fixes"][0]["strategy"] == "relax"
    assert data["fixes"][0]["before"] == 10
    assert data["fixes"][0]["after"] == 20
    assert data["fixes"][0]["source_discrepancy"]["domain"] == "test_domain"
    assert data["fixes"][0]["source_discrepancy"]["method"] == "POST"
    assert data["fixes"][0]["source_discrepancy"]["spec_file"] == "test_spec.json"

    fixed_spec = json.loads((out / "test_spec.json").read_text())
    assert fixed_spec["components"]["schemas"]["TestModel"]["maxLength"] == 20


def test_reconcile_validation_rollback(setup_reconcile_env):
    """OpenAPI validation rollback produces one validate failure for each affected discrepancy and nonzero exit."""
    run, reports, out, config = setup_reconcile_env
    specs = {
        "test_spec.json": {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
            "components": {"schemas": {"TestModel": {"type": "string", "maxLength": 10}}},
        }
    }

    discrepancies = [
        _make_discrepancy(
            spec_file="test_spec.json",
            path="TestModel",
            property_name="TestModel",
            constraint_type="type",
            discrepancy_type="spec_stricter",
            api_behavior="invalid_type_name_forces_rollback",
            domain="rollback_domain",
            method="GET",
        )
    ]

    ret = run(discrepancies, specs)
    assert ret == 1

    report_file = Path("reports/reconciliation_report.json")
    assert report_file.exists()

    data = json.loads(report_file.read_text())
    validate_reconciliation_report(data)

    assert data["summary"]["fixes_applied"] == 0
    assert data["summary"]["failures"] == 1
    assert data["failures"][0]["stage"] == "validate"
    assert "OpenAPI validation failed" in data["failures"][0]["error"]
    assert data["failures"][0]["source_discrepancy"]["domain"] == "rollback_domain"
    assert data["failures"][0]["source_discrepancy"]["method"] == "GET"


def test_missing_validation_report_fails(setup_reconcile_env):
    """Missing validation report returns nonzero and produces no healthy report."""
    run, reports, out, config = setup_reconcile_env
    specs: dict[str, Any] = {}

    ret = run(None, specs, simulate_missing_report=True)
    assert ret == 1

    report_file = Path("reports/reconciliation_report.json")
    assert not report_file.exists()


def test_report_without_discrepancies_array_fails(setup_reconcile_env):
    """A report without discrepancies array returns nonzero."""
    run, reports, out, config = setup_reconcile_env
    specs: dict[str, Any] = {}

    ret = run({"foo": "bar"}, specs)
    assert ret == 1

    report_file = Path("reports/reconciliation_report.json")
    assert not report_file.exists()


def test_malformed_discrepancy_fields_fail(setup_reconcile_env):
    """Malformed discrepancy fields return nonzero."""
    run, reports, out, config = setup_reconcile_env
    specs: dict[str, Any] = {}

    # Missing required 'path' field, will violate ValueError parsing and structural check
    discrepancies = [
        {
            "spec_file": "test_spec.json",
            "property_name": "TestModel",
            "constraint_type": "maxLength",
            "discrepancy_type": "spec_stricter",
        }
    ]

    ret = run(discrepancies, specs)
    assert ret == 1

    report_file = Path("reports/reconciliation_report.json")
    assert not report_file.exists()


def test_unknown_spec_file_fails_match(setup_reconcile_env):
    """An unknown spec_file produces a match failure and nonzero exit."""
    run, reports, out, config = setup_reconcile_env
    specs = {
        "test_spec.json": {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
        }
    }

    # spec_file points to nonexistent.json
    discrepancies = [
        _make_discrepancy(
            spec_file="nonexistent.json",
            path="TestModel",
            property_name="TestModel",
            constraint_type="maxLength",
            discrepancy_type="spec_stricter",
            api_behavior=10,
            domain="test",
            method="POST",
        )
    ]

    ret = run(discrepancies, specs)
    assert ret == 1

    report_file = Path("reports/reconciliation_report.json")
    assert report_file.exists()

    data = json.loads(report_file.read_text())
    validate_reconciliation_report(data)

    assert data["summary"]["processed_specs"] == 1
    assert data["summary"]["failures"] == 1
    assert data["failures"][0]["stage"] == "match"
    assert "nonexistent.json" in data["failures"][0]["error"]


def test_unsupported_strategy_fails_apply(setup_reconcile_env):
    """Unsupported strategy or no-op mutation produces an apply failure and nonzero exit."""
    run, reports, out, config = setup_reconcile_env
    specs = {
        "test_spec.json": {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
            "components": {"schemas": {"TestModel": {"type": "string", "maxLength": 10}}},
        }
    }

    discrepancies = [
        _make_discrepancy(
            spec_file="test_spec.json",
            path="TestModel",
            property_name="TestModel",
            constraint_type="maxLength",
            discrepancy_type="spec_stricter",
            spec_value=10,
            api_behavior=10,  # no-op!
            domain="test",
            method="POST",
        )
    ]

    ret = run(discrepancies, specs)
    assert ret == 1

    report_file = Path("reports/reconciliation_report.json")
    assert report_file.exists()

    data = json.loads(report_file.read_text())
    validate_reconciliation_report(data)

    assert data["summary"]["failures"] == 1
    assert data["failures"][0]["stage"] == "apply"
    assert "no-op" in data["failures"][0]["error"]


def test_mixed_fixes_and_failures_preserve_all_outcomes(setup_reconcile_env):
    """Mixed fixes and failures preserve all outcomes and return nonzero."""
    run, reports, out, config = setup_reconcile_env
    specs = {
        "test_spec.json": {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
            "components": {"schemas": {"TestModel": {"type": "string", "maxLength": 10}}},
        }
    }

    discrepancies = [
        _make_discrepancy(
            spec_file="test_spec.json",
            path="TestModel",
            property_name="TestModel",
            constraint_type="maxLength",
            discrepancy_type="spec_stricter",
            spec_value=10,
            api_behavior=20,  # Successful relax fix
            domain="test",
            method="POST",
        ),
        _make_discrepancy(
            spec_file="test_spec.json",
            path="TestModel",
            property_name="TestModel",
            constraint_type="maxLength",
            discrepancy_type="spec_stricter",
            spec_value=10,
            api_behavior=10,  # No-op apply failure
            domain="test",
            method="POST",
        ),
    ]

    ret = run(discrepancies, specs)
    assert ret == 1

    report_file = Path("reports/reconciliation_report.json")
    assert report_file.exists()

    data = json.loads(report_file.read_text())
    validate_reconciliation_report(data)

    assert data["summary"]["fixes_applied"] == 1
    assert data["summary"]["failures"] == 1
    assert len(data["fixes"]) == 1
    assert len(data["failures"]) == 1


def test_custom_reconciliation_report_out_honored(setup_reconcile_env):
    """Custom --reconciliation-report-out is honored."""
    run, reports, out, config = setup_reconcile_env
    specs = {
        "test_spec.json": {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
        }
    }

    custom_out = "reports/custom_report.json"
    ret = run([], specs, report_out=custom_out)
    assert ret == 0

    default_report = Path("reports/reconciliation_report.json")
    custom_report = Path(custom_out)

    assert not default_report.exists()
    assert custom_report.exists()

    data = json.loads(custom_report.read_text())
    validate_reconciliation_report(data)


def test_load_discrepancies_filename_validation(tmp_path):
    """Verify that load_discrepancies allows valid filenames and rejects invalid ones."""

    # 1. Valid filename
    valid_report = tmp_path / "valid_report.json"
    valid_data = {"discrepancies": [_make_discrepancy(spec_file="valid-filename.json")]}
    valid_report.write_text(json.dumps(valid_data))
    res = load_discrepancies(valid_report)
    assert len(res) == 1
    assert res[0].spec_file == "valid-filename.json"

    # 2. Invalid filenames with control characters or markdown injections
    invalid_cases = [
        "bad\nfile.json",
        "bad\rfile.json",
        "bad/file.json",
        "../bad.json",
        "bad`file.json",
        "bad#file.json",
        "bad{file}.json",
    ]

    for idx, filename in enumerate(invalid_cases):
        invalid_report = tmp_path / f"invalid_report_{idx}.json"
        invalid_data = {"discrepancies": [_make_discrepancy(spec_file=filename)]}
        invalid_report.write_text(json.dumps(invalid_data))
        with pytest.raises(ValueError, match="invalid characters"):
            load_discrepancies(invalid_report)


def test_unknown_spec_file_not_double_accounted(setup_reconcile_env):
    """Verify that discrepancies with spec_file == 'unknown' do not cause double accounting."""
    run, reports, out, config = setup_reconcile_env
    specs = {
        "test_spec.json": {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
        }
    }

    # A discrepancy with spec_file == "unknown" but path that split-matches "test_spec.json"
    discrepancies = [
        _make_discrepancy(
            spec_file="unknown",
            path="test_spec.json#/components/schemas/foo",
            property_name="foo",
            constraint_type="maxLength",
            discrepancy_type="spec_stricter",
            spec_value=10,
            api_behavior=20,
            domain="test",
            method="POST",
        )
    ]

    ret = run(discrepancies, specs)
    # Since it is a match failure (spec_file == "unknown"), it is recorded in match stage
    # and not processed in _group_by_file, so it doesn't try to apply/write any fix.
    # It exits with 1 (because failures occurred during reconciliation, i.e. match failure).
    assert ret == 1

    report_file = Path("reports/reconciliation_report.json")
    assert report_file.exists()

    data = json.loads(report_file.read_text())
    validate_reconciliation_report(data)

    # It should be listed exactly once in the failures (with stage: match) and 0 in fixes
    assert data["summary"]["fixes_applied"] == 0
    assert data["summary"]["failures"] == 1
    assert len(data["fixes"]) == 0
    assert len(data["failures"]) == 1
    assert data["failures"][0]["stage"] == "match"
