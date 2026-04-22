"""Tests for the ReportGenerator JSON output shape."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.utils.constraint_validator import Discrepancy, DiscrepancyType
from scripts.utils.report_generator import ReportConfig, ReportGenerator
from scripts.validate import _domain_from_filename


def test_json_report_emits_domain_and_method_per_discrepancy(tmp_path: Path) -> None:
    """generate_all threads discrepancy_domains/methods into each JSON entry."""
    config = ReportConfig(output_dir=tmp_path, formats=["json"])
    gen = ReportGenerator(config)
    d = Discrepancy(
        path="/public/config/namespaces/system/origin_pools",
        property_name="port",
        constraint_type="minimum",
        discrepancy_type=DiscrepancyType.SPEC_STRICTER,
        spec_value=1,
        api_behavior={"accepted": 0},
        test_values=[0],
    )
    gen.generate_all(
        results=[],
        discrepancies=[d],
        modified_files=[],
        unmodified_files=[],
        discrepancy_domains=["origin_pool"],
        discrepancy_methods=["POST"],
    )
    data = json.loads((tmp_path / "validation_report.json").read_text())
    entry = data["discrepancies"][0]
    assert entry["domain"] == "origin_pool"
    assert entry["method"] == "POST"
    # Verify the other keys issue_sync._load_discrepancies needs are present too.
    assert entry["property_name"] == "port"
    assert entry["constraint_type"] == "minimum"
    assert entry["discrepancy_type"] == "spec_stricter"
    assert entry["test_values"] == [0]


def test_domain_from_filename_extracts_slug() -> None:
    """The filename-to-domain-slug mapping handles F5 XC naming conventions."""
    assert (
        _domain_from_filename(
            "docs-cloud-f5-com.0041.public.ves.io.schema.origin_pool.ves-swagger.json"
        )
        == "origin_pool"
    )
    assert (
        _domain_from_filename(
            "docs-cloud-f5-com.0001.public.ves.io.schema.api_sec.api_crawler."
            "ves-swagger.json"
        )
        == "api_sec.api_crawler"
    )
    # Filenames that don't match fall back to the stem, not an error.
    assert _domain_from_filename("some-other.json") == "some-other"
    assert _domain_from_filename("") == "unknown"


def test_report_generator_tolerates_missing_parallel_lists(tmp_path: Path) -> None:
    """If a caller hasn't been updated yet, default domain/method to 'unknown'."""
    config = ReportConfig(output_dir=tmp_path, formats=["json"])
    gen = ReportGenerator(config)
    d = Discrepancy(
        path="/x",
        property_name="p",
        constraint_type="minimum",
        discrepancy_type=DiscrepancyType.SPEC_STRICTER,
        spec_value=1,
        api_behavior={},
        test_values=[0],
    )
    gen.generate_all(
        results=[],
        discrepancies=[d],
        modified_files=[],
        unmodified_files=[],
    )  # no discrepancy_domains / discrepancy_methods kwargs
    data = json.loads((tmp_path / "validation_report.json").read_text())
    entry = data["discrepancies"][0]
    assert entry["domain"] == "unknown"
    assert entry["method"] == "unknown"
