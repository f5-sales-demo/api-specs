"""Tests for the VALIDATION_REPORT.md builder in scripts/release.py.

The builder reads a validation_report.json (produced by
ReportGenerator) and, when an issue_mapping.json is supplied, annotates
each discrepancy row with a link to the tracking GitHub issue.
"""

from __future__ import annotations

import json

from scripts.release import build_validation_report_md
from scripts.utils.constraint_validator import Discrepancy, DiscrepancyType
from scripts.utils.discrepancy_fingerprint import fingerprint


def test_validation_report_contains_tracked_as_issues_column(tmp_path):
    """When a mapping entry exists, the row links to its GitHub issue."""
    validation_report = tmp_path / "validation_report.json"
    validation_report.write_text(
        json.dumps(
            {
                "summary": {
                    "timestamp": "2026-04-21T00:00:00+00:00",
                    "total_endpoints": 1,
                    "total_tests": 1,
                    "passed": 0,
                    "failed": 1,
                    "errors": 0,
                    "total_discrepancies": 1,
                },
                "discrepancies": [
                    {
                        "path": "/public/config/namespaces/system/origin_pools",
                        "property_name": "port",
                        "constraint_type": "minimum",
                        "discrepancy_type": "spec_stricter",
                        "domain": "origin_pool",
                        "method": "POST",
                        "spec_value": 1,
                        "api_behavior": {"accepted": 0},
                        "test_values": [0],
                    }
                ],
            }
        )
    )

    # Compute the fingerprint with the same inputs the builder will use.
    fp = fingerprint(
        Discrepancy(
            path="/public/config/namespaces/system/origin_pools",
            property_name="port",
            constraint_type="minimum",
            discrepancy_type=DiscrepancyType.SPEC_STRICTER,
            spec_value=1,
            api_behavior={"accepted": 0},
            test_values=[0],
        ),
        "origin_pool",
        "POST",
    )
    mapping = {
        fp: {
            "action": "created",
            "issue_number": 42,
            "issue_url": "https://github.com/x/y/issues/42",
        }
    }
    issue_mapping = tmp_path / "issue_mapping.json"
    issue_mapping.write_text(json.dumps(mapping))

    md = build_validation_report_md(validation_report, issue_mapping)

    assert "Tracked as issues" in md
    assert "#42" in md
    assert "https://github.com/x/y/issues/42" in md


def test_validation_report_renders_em_dash_when_no_issue_mapped(tmp_path):
    """Rows without a mapping entry show an em-dash in the issue column."""
    validation_report = tmp_path / "validation_report.json"
    validation_report.write_text(
        json.dumps(
            {
                "discrepancies": [
                    {
                        "path": "/x",
                        "property_name": "p",
                        "constraint_type": "minimum",
                        "discrepancy_type": "spec_stricter",
                        "domain": "origin_pool",
                        "method": "POST",
                        "spec_value": 1,
                        "api_behavior": {},
                        "test_values": [0],
                    }
                ]
            }
        )
    )

    md = build_validation_report_md(validation_report, None)

    assert "Tracked as issues" in md
    assert "—" in md  # em-dash for unmapped rows
