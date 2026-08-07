import json
import pytest
from pathlib import Path
from scripts.utils.strict_data import StrictDataError

# Try to import our utility. Since we work test-first, this will fail if the module isn't implemented or has issues.
try:
    from scripts.utils.reconciliation_report import (
        validate_reconciliation_report,
        load_reconciliation_report,
        write_reconciliation_report,
        ReconciliationReportError,
    )
except ImportError:
    # During TDD red phase, we allow import failure so that we can verify the red state.
    validate_reconciliation_report = None
    load_reconciliation_report = None
    write_reconciliation_report = None
    class ReconciliationReportError(Exception):
        pass


def test_tdd_red_assert_defined():
    """Ensure the imports are defined, acting as a sentinel for TDD transition."""
    assert validate_reconciliation_report is not None
    assert load_reconciliation_report is not None
    assert write_reconciliation_report is not None


@pytest.fixture
def valid_report_dict():
    return {
        "schema_version": 1,
        "generated_at": "2026-08-07T12:00:00+00:00",
        "summary": {
            "processed_specs": 2,
            "discrepancies_received": 2,
            "fixes_applied": 1,
            "failures": 1,
            "modified_files": ["example.json"],
            "unmodified_files": ["other.json"]
        },
        "fixes": [
            {
                "spec_file": "example.json",
                "strategy": "relax",
                "before": 10,
                "after": 20,
                "source_discrepancy": {
                    "spec_file": "example.json",
                    "path": "...",
                    "property_name": "...",
                    "constraint_type": "maxLength",
                    "discrepancy_type": "spec_stricter",
                    "spec_value": 10,
                    "api_behavior": 20,
                    "test_values": [],
                    "recommendation": "...",
                    "domain": "...",
                    "method": "POST"
                }
            }
        ],
        "failures": [
            {
                "spec_file": "other.json",
                "stage": "match",
                "error": "Some error",
                "source_discrepancy": {
                    "spec_file": "other.json",
                    "path": "...",
                    "property_name": "...",
                    "constraint_type": "maxLength",
                    "discrepancy_type": "spec_stricter",
                    "spec_value": 10,
                    "api_behavior": 20,
                    "test_values": [],
                    "recommendation": "...",
                    "domain": "...",
                    "method": "POST"
                }
            }
        ]
    }


def test_valid_report_passes(valid_report_dict):
    if validate_reconciliation_report is None:
        pytest.skip("Module not implemented yet")
    validate_reconciliation_report(valid_report_dict)


def test_invalid_schema_fails(valid_report_dict):
    if validate_reconciliation_report is None:
        pytest.skip("Module not implemented yet")
    
    # Missing required root fields
    bad = valid_report_dict.copy()
    del bad["schema_version"]
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_schema_version_not_one_fails(valid_report_dict):
    if validate_reconciliation_report is None:
        pytest.skip("Module not implemented yet")
    bad = valid_report_dict.copy()
    bad["schema_version"] = 2
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_invalid_additional_properties_fails(valid_report_dict):
    if validate_reconciliation_report is None:
        pytest.skip("Module not implemented yet")
    bad = valid_report_dict.copy()
    bad["extra_property"] = "invalid"
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_invalid_strategy_fails(valid_report_dict):
    if validate_reconciliation_report is None:
        pytest.skip("Module not implemented yet")
    bad = json.loads(json.dumps(valid_report_dict))
    bad["fixes"][0]["strategy"] = "invalid_strategy"
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_invalid_stage_fails(valid_report_dict):
    if validate_reconciliation_report is None:
        pytest.skip("Module not implemented yet")
    bad = json.loads(json.dumps(valid_report_dict))
    bad["failures"][0]["stage"] = "invalid_stage"
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_negative_counts_fail(valid_report_dict):
    if validate_reconciliation_report is None:
        pytest.skip("Module not implemented yet")
    bad = json.loads(json.dumps(valid_report_dict))
    bad["summary"]["processed_specs"] = -1
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_count_mismatches_fail(valid_report_dict):
    if validate_reconciliation_report is None:
        pytest.skip("Module not implemented yet")
    
    # summary.fixes_applied != len(fixes)
    bad = json.loads(json.dumps(valid_report_dict))
    bad["summary"]["fixes_applied"] = 0
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)

    # summary.failures != len(failures)
    bad = json.loads(json.dumps(valid_report_dict))
    bad["summary"]["failures"] = 0
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)

    # summary.discrepancies_received != len(fixes) + len(failures)
    bad = json.loads(json.dumps(valid_report_dict))
    bad["summary"]["discrepancies_received"] = 5
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_file_partitions_fail_when_overlapping(valid_report_dict):
    if validate_reconciliation_report is None:
        pytest.skip("Module not implemented yet")
    bad = json.loads(json.dumps(valid_report_dict))
    # 'example.json' in both modified and unmodified lists
    bad["summary"]["modified_files"] = ["example.json"]
    bad["summary"]["unmodified_files"] = ["example.json"]
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_differing_filenames_fail(valid_report_dict):
    if validate_reconciliation_report is None:
        pytest.skip("Module not implemented yet")
    bad = json.loads(json.dumps(valid_report_dict))
    # top level spec_file is "example.json" but source discrepancy is "other.json"
    bad["fixes"][0]["spec_file"] = "example.json"
    bad["fixes"][0]["source_discrepancy"]["spec_file"] = "other.json"
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_modified_unmodified_union_and_fixes_invariants(valid_report_dict):
    if validate_reconciliation_report is None:
        pytest.skip("Module not implemented yet")
    
    bad = json.loads(json.dumps(valid_report_dict))
    # Fix references "example.json", but "example.json" is not in modified_files
    bad["summary"]["modified_files"] = []
    bad["summary"]["unmodified_files"] = ["example.json", "other.json"]
    # Adjust counts to keep them consistent
    bad["summary"]["fixes_applied"] = 1
    bad["summary"]["failures"] = 1
    bad["summary"]["discrepancies_received"] = 2
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_unmodified_files_cannot_have_fixes(valid_report_dict):
    if validate_reconciliation_report is None:
        pytest.skip("Module not implemented yet")
    
    bad = json.loads(json.dumps(valid_report_dict))
    # 'example.json' is in unmodified list, but has a fix entry
    bad["summary"]["modified_files"] = []
    bad["summary"]["unmodified_files"] = ["example.json", "other.json"]
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_duplicate_keys_fail_loads(tmp_path):
    if load_reconciliation_report is None:
        pytest.skip("Module not implemented yet")
    
    # Write report with duplicate keys
    file_path = tmp_path / "dup_keys.json"
    file_path.write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(StrictDataError):
        load_reconciliation_report(file_path)


def test_non_finite_values_fail_loads(tmp_path):
    if load_reconciliation_report is None:
        pytest.skip("Module not implemented yet")
    
    file_path = tmp_path / "non_finite.json"
    file_path.write_text('{"schema_version":1,"generated_at":"2026-08-07T12:00:00+00:00","fixes":[],"failures":[],"summary":{"processed_specs":0,"discrepancies_received":0,"fixes_applied":0,"failures":0,"modified_files":[],"unmodified_files":[]},"bad_val": NaN}')
    with pytest.raises(StrictDataError):
        load_reconciliation_report(file_path)
