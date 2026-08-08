import json

import pytest

from scripts.utils.reconciliation_report import (
    ReconciliationReportError,
    load_reconciliation_report,
    validate_reconciliation_report,
    write_reconciliation_report,
)


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
            "unmodified_files": ["other.json"],
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
                    "method": "POST",
                },
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
                    "method": "POST",
                },
            }
        ],
    }


def test_valid_report_passes(valid_report_dict):
    validate_reconciliation_report(valid_report_dict)


def test_invalid_schema_fails(valid_report_dict):
    # Missing required root fields
    bad = valid_report_dict.copy()
    del bad["schema_version"]
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_schema_version_not_one_fails(valid_report_dict):
    bad = valid_report_dict.copy()
    bad["schema_version"] = 2
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_invalid_additional_properties_fails(valid_report_dict):
    bad = valid_report_dict.copy()
    bad["extra_property"] = "invalid"
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_invalid_strategy_fails(valid_report_dict):
    bad = json.loads(json.dumps(valid_report_dict))
    bad["fixes"][0]["strategy"] = "invalid_strategy"
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_invalid_stage_fails(valid_report_dict):
    bad = json.loads(json.dumps(valid_report_dict))
    bad["failures"][0]["stage"] = "invalid_stage"
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_negative_counts_fail(valid_report_dict):
    bad = json.loads(json.dumps(valid_report_dict))
    bad["summary"]["processed_specs"] = -1
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_count_mismatches_fail(valid_report_dict):
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
    bad = json.loads(json.dumps(valid_report_dict))
    # 'example.json' in both modified and unmodified lists
    bad["summary"]["modified_files"] = ["example.json"]
    bad["summary"]["unmodified_files"] = ["example.json"]
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_differing_filenames_fail(valid_report_dict):
    bad = json.loads(json.dumps(valid_report_dict))
    # top level spec_file is "example.json" but source discrepancy is "other.json"
    bad["fixes"][0]["spec_file"] = "example.json"
    bad["fixes"][0]["source_discrepancy"]["spec_file"] = "other.json"
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_modified_unmodified_union_and_fixes_invariants(valid_report_dict):
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
    bad = json.loads(json.dumps(valid_report_dict))
    # 'example.json' is in unmodified list, but has a fix entry
    bad["summary"]["modified_files"] = []
    bad["summary"]["unmodified_files"] = ["example.json", "other.json"]
    with pytest.raises(ReconciliationReportError):
        validate_reconciliation_report(bad)


def test_duplicate_keys_fail_loads(tmp_path):
    # Write report with duplicate keys
    file_path = tmp_path / "dup_keys.json"
    file_path.write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(ReconciliationReportError):
        load_reconciliation_report(file_path)


def test_non_finite_values_fail_loads(tmp_path):
    file_path = tmp_path / "non_finite.json"
    file_path.write_text(
        '{"schema_version":1,"generated_at":"2026-08-07T12:00:00+00:00","fixes":[],"failures":[],"summary":{"processed_specs":0,"discrepancies_received":0,"fixes_applied":0,"failures":0,"modified_files":[],"unmodified_files":[]},"bad_val": NaN}'
    )
    with pytest.raises(ReconciliationReportError):
        load_reconciliation_report(file_path)


def test_write_reconciliation_report_rejects_nan_and_infinities(tmp_path, valid_report_dict):
    # NaN in a nested field
    bad = json.loads(json.dumps(valid_report_dict))
    bad["fixes"][0]["before"] = float("nan")
    file_path = tmp_path / "test_nan.json"
    with pytest.raises(ReconciliationReportError):
        write_reconciliation_report(bad, file_path)
    assert not file_path.exists()

    # Infinity in nested field
    bad = json.loads(json.dumps(valid_report_dict))
    bad["fixes"][0]["before"] = float("inf")
    file_path = tmp_path / "test_inf.json"
    with pytest.raises(ReconciliationReportError):
        write_reconciliation_report(bad, file_path)
    assert not file_path.exists()

    # -Infinity in nested field
    bad = json.loads(json.dumps(valid_report_dict))
    bad["fixes"][0]["before"] = float("-inf")
    file_path = tmp_path / "test_neginf.json"
    with pytest.raises(ReconciliationReportError):
        write_reconciliation_report(bad, file_path)
    assert not file_path.exists()


def test_write_rejected_does_not_replace_pre_existing_valid_report(tmp_path, valid_report_dict):
    file_path = tmp_path / "existing.json"
    write_reconciliation_report(valid_report_dict, file_path)
    assert file_path.exists()
    original_content = file_path.read_text(encoding="utf-8")

    # Attempt to write invalid report with NaN
    bad = json.loads(json.dumps(valid_report_dict))
    bad["fixes"][0]["before"] = float("nan")
    with pytest.raises(ReconciliationReportError):
        write_reconciliation_report(bad, file_path)

    # Check that the pre-existing report is unmodified
    assert file_path.read_text(encoding="utf-8") == original_content


@pytest.mark.parametrize("invalid_filename", [
    "bad\nfilename.json",
    "bad\rfilename.json",
    "bad/filename.json",
    "../traversal.json",
    "# heading.json",
    "braces{heading}.json",
    "angle<brackets>.json",
    ""
])
def test_schema_filename_validation_negative(valid_report_dict, invalid_filename):
    # Test modified_files negative validation
    bad_report = json.loads(json.dumps(valid_report_dict))
    bad_report["summary"]["modified_files"].append(invalid_filename)
    # Adjust processed_specs count to match union length since disjoint-union is checked semantically
    bad_report["summary"]["processed_specs"] = len(bad_report["summary"]["modified_files"]) + len(bad_report["summary"]["unmodified_files"])
    with pytest.raises(ReconciliationReportError, match="Schema validation failed"):
        validate_reconciliation_report(bad_report)

    # Test unmodified_files negative validation
    bad_report = json.loads(json.dumps(valid_report_dict))
    bad_report["summary"]["unmodified_files"].append(invalid_filename)
    bad_report["summary"]["processed_specs"] = len(bad_report["summary"]["modified_files"]) + len(bad_report["summary"]["unmodified_files"])
    with pytest.raises(ReconciliationReportError, match="Schema validation failed"):
        validate_reconciliation_report(bad_report)

    # Test fixes negative validation
    bad_report = json.loads(json.dumps(valid_report_dict))
    bad_report["fixes"][0]["spec_file"] = invalid_filename
    # Fix the source_discrepancy spec_file to match to avoid triggering semantic check before schema validation
    bad_report["fixes"][0]["source_discrepancy"]["spec_file"] = invalid_filename
    if invalid_filename not in bad_report["summary"]["modified_files"]:
        bad_report["summary"]["modified_files"].append(invalid_filename)
    bad_report["summary"]["processed_specs"] = len(bad_report["summary"]["modified_files"]) + len(bad_report["summary"]["unmodified_files"])
    with pytest.raises(ReconciliationReportError, match="Schema validation failed"):
        validate_reconciliation_report(bad_report)


def test_schema_filename_validation_positive(valid_report_dict):
    # Confirm that valid alphanumeric and allowed characters (._-) pass validation
    good_report = json.loads(json.dumps(valid_report_dict))
    valid_filenames = ["spec.json", "spec_1.json", "spec-2.json", "spec.name.json"]
    for fname in valid_filenames:
        good_report["fixes"][0]["spec_file"] = fname
        good_report["fixes"][0]["source_discrepancy"]["spec_file"] = fname
        good_report["summary"]["modified_files"] = [fname]
        good_report["summary"]["processed_specs"] = len(good_report["summary"]["modified_files"]) + len(good_report["summary"]["unmodified_files"])
        validate_reconciliation_report(good_report)

