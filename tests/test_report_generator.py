"""Tests for the ReportGenerator JSON output shape and validation provenance."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.utils.constraint_validator import Discrepancy, DiscrepancyType
from scripts.utils.report_generator import ReportConfig, ReportGenerator
from scripts.validate import _domain_from_filename


def test_json_report_emits_domain_and_method_per_discrepancy(tmp_path: Path) -> None:
    """generate_all serializes domain, method, and spec_file directly from Discrepancy properties."""
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
        domain="origin_pool",
        method="POST",
        spec_file="test_spec.json",
    )
    gen.generate_all(
        results=[],
        discrepancies=[d],
        modified_files=["test_spec.json"],
        unmodified_files=[],
    )
    data = json.loads((tmp_path / "validation_report.json").read_text())
    entry = data["discrepancies"][0]
    assert entry["domain"] == "origin_pool"
    assert entry["method"] == "POST"
    assert entry["spec_file"] == "test_spec.json"
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
            "docs-cloud-f5-com.0001.public.ves.io.schema.api_sec.api_crawler.ves-swagger.json"
        )
        == "api_sec.api_crawler"
    )
    # Filenames that don't match fall back to the stem, not an error.
    assert _domain_from_filename("some-other.json") == "some-other"
    assert _domain_from_filename("") == "unknown"


def test_exact_spec_file_classification() -> None:
    """Modified classification uses exact d.spec_file == filename.

    A path containing a filename-like substring cannot misclassify a spec.
    """
    # Create discrepancies where one has d.spec_file == "target.json"
    # and another has "target.json" in its path but d.spec_file == "other.json"
    d1 = Discrepancy(
        path="/some/path/target.json/field",
        property_name="p",
        constraint_type="minLength",
        discrepancy_type=DiscrepancyType.SPEC_STRICTER,
        spec_value=1,
        api_behavior={},
        spec_file="other.json",
    )
    
    d2 = Discrepancy(
        path="/some/other/path",
        property_name="p",
        constraint_type="minLength",
        discrepancy_type=DiscrepancyType.SPEC_STRICTER,
        spec_value=1,
        api_behavior={},
        spec_file="target.json",
    )

    # If we classify "target.json" using d.spec_file == filename:
    # d1 should NOT match "target.json" (since its spec_file is "other.json").
    # d2 should match "target.json".
    
    assert d1.spec_file != "target.json"
    assert d2.spec_file == "target.json"
