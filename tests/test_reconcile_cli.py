"""Tests for the reconcile.py CLI behavior."""

import json
from pathlib import Path

import jsonschema
import pytest

from scripts.reconcile import main as reconcile_main


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
    
    def _run(discrepancies: list, specs: dict[str, dict]) -> int:
        val_report = reports_dir / "validation_report.json"
        val_report.write_text(json.dumps({"discrepancies": discrepancies}))
        
        for name, content in specs.items():
            (original_dir / name).write_text(json.dumps(content))
            
        monkeypatch.setattr(
            "sys.argv",
            [
                "reconcile.py",
                "--report", str(val_report),
                "--original-dir", str(original_dir),
                "--output-dir", str(output_dir),
            ]
        )
        return reconcile_main()

    return _run, reports_dir, output_dir, config_dir


def test_reconcile_zero_fixes(setup_reconcile_env):
    run, reports, out, config = setup_reconcile_env
    specs = {
        "test_spec.json": {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {}
        }
    }
    ret = run([], specs)
    assert ret == 0
    
    report_file = reports / "reconciliation_report.json"
    assert report_file.exists()
    
    data = json.loads(report_file.read_text())
    jsonschema.validate(instance=data, schema=json.loads((config / "reconciliation_report.schema.json").read_text()))
    
    assert data["summary"]["processed_specs"] == 1
    assert data["summary"]["fixes_applied"] == 0
    assert data["fixes"] == []


def test_reconcile_successful_fix(setup_reconcile_env):
    run, reports, out, config = setup_reconcile_env
    specs = {
        "test_spec.json": {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
            "components": {
                "schemas": {
                    "TestModel": {
                        "type": "string",
                        "maxLength": 10
                    }
                }
            }
        }
    }
    
    discrepancies = [{
        "spec_file": "test_spec.json",
        "path": "TestModel",
        "property_name": "TestModel",
        "constraint_type": "maxLength",
        "discrepancy_type": "spec_stricter",
        "spec_value": 10,
        "api_behavior": 20
    }]
    
    ret = run(discrepancies, specs)
    assert ret == 0
    
    report_file = reports / "reconciliation_report.json"
    data = json.loads(report_file.read_text())
    jsonschema.validate(instance=data, schema=json.loads((config / "reconciliation_report.schema.json").read_text()))
    
    assert data["summary"]["fixes_applied"] == 1
    assert data["fixes"][0]["strategy"] == "relax"
    assert data["fixes"][0]["before"] == 10
    assert data["fixes"][0]["after"] == 20
    
    fixed_spec = json.loads((out / "test_spec.json").read_text())
    assert fixed_spec["components"]["schemas"]["TestModel"]["maxLength"] == 20


def test_reconcile_validation_rollback(setup_reconcile_env):
    run, reports, out, config = setup_reconcile_env
    specs = {
        "test_spec.json": {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {},
            "components": {
                "schemas": {
                    "TestModel": {
                        "type": "string",
                        "maxLength": 10
                    }
                }
            }
        }
    }
    
    discrepancies = [{
        "spec_file": "test_spec.json",
        "path": "TestModel",
        "property_name": "TestModel",
        "constraint_type": "type",
        "discrepancy_type": "spec_stricter",
        "api_behavior": "invalid_type_name_forces_rollback"
    }]
    
    ret = run(discrepancies, specs)
    assert ret == 0
    
    report_file = reports / "reconciliation_report.json"
    data = json.loads(report_file.read_text())
    jsonschema.validate(instance=data, schema=json.loads((config / "reconciliation_report.schema.json").read_text()))
    
    assert data["summary"]["fixes_applied"] == 0
    assert data["summary"]["failures"] == 1
    assert data["failures"][0]["stage"] == "validate"
