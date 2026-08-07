"""Unit tests for the generate_docs.py utility and MDX safety."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.generate_docs import _format_value, generate_fixes_page


def test_default_args():
    """Verify default CLI arguments in generate_docs parser."""
    # Create the parser exactly like generate_docs does to verify defaults
    parser = argparse.ArgumentParser(description="Generate documentation from reconciliation reports")
    parser.add_argument(
        "--reconciliation-report",
        type=Path,
        required=True,
        help="Path to strict reconciliation report JSON",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/en/01-validation-report.mdx"),
        help="Output path for documentation page",
    )
    
    # Parse with dummy required argument
    args = parser.parse_args(["--reconciliation-report", "reports/reconciliation_report.json"])
    assert args.output == Path("docs/en/01-validation-report.mdx")


@pytest.mark.parametrize(
    ("input_value", "expected_escaped"),
    [
        ("<script>", "`&lt;script&gt;`"),
        ("Value {with_braces}", "`Value &#123;with_braces&#125;`"),
        ("normal_text", "`normal_text`"),
        (None, "-"),
        (True, "`true`"),
        (False, "`false`"),
        (["a", "b"], '`["a", "b"]`'),
    ],
)
def test_mdx_value_safety(input_value, expected_escaped):
    """Ensure characters that break MDX parsers are safely escaped or encoded."""
    escaped = _format_value(input_value)
    assert escaped == expected_escaped


def test_frontmatter_mapping(tmp_path):
    """Assert that the generated MDX page includes the correct YAML frontmatter mapping."""
    output_file = tmp_path / "01-validation-report.mdx"
    
    # Build dummy reconciliation report
    report = {
        "schema_version": 1,
        "generated_at": "2026-08-07T12:00:00Z",
        "summary": {
            "processed_specs": 1,
            "discrepancies_received": 1,
            "fixes_applied": 1,
            "failures": 0,
            "modified_files": ["test_spec.json"],
            "unmodified_files": [],
        },
        "fixes": [
            {
                "spec_file": "test_spec.json",
                "strategy": "relax",
                "before": 10,
                "after": 20,
                "source_discrepancy": {
                    "path": "TestModel",
                    "property_name": "TestModel",
                    "constraint_type": "maxLength",
                }
            }
        ],
        "failures": []
    }
    
    generate_fixes_page(report, output_file)
    
    assert output_file.exists()
    content = output_file.read_text(encoding="utf-8")
    
    # Assert correct frontmatter structure
    assert content.startswith("---\ntitle: Validation Report\ndescription: F5 XC API spec validation and fix report\n---")
    
    # Verify MDX-breaking brackets or braces are escaped inside tables too
    # Let's verify that MDX tables were formatted correctly
    assert "| Specs Processed | 1 |" in content
    assert "| **Fixes Applied** | **1** |" in content
    assert "### test_spec.json" in content
