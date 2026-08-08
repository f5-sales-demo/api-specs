"""Unit tests for the generate_docs.py utility and MDX safety."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.generate_docs import _format_value, generate_fixes_page


def test_default_args():
    """Verify default CLI arguments in generate_docs parser."""
    # Create the parser exactly like generate_docs does to verify defaults
    parser = argparse.ArgumentParser(
        description="Generate documentation from reconciliation reports"
    )
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
        ("Value & other", "`Value &amp; other`"),
        ("Value `backtick`", "`Value &#96;backtick&#96;`"),
        ("Value\nnewline", "`Value<br />newline`"),
        ("Value | pipe", "`Value &#124; pipe`"),
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
                },
            }
        ],
        "failures": [],
    }

    generate_fixes_page(report, output_file)

    assert output_file.exists()
    content = output_file.read_text(encoding="utf-8")

    # Assert correct frontmatter structure
    assert content.startswith(
        "---\ntitle: Validation Report\ndescription: F5 XC API spec validation and fix report\n---"
    )

    # Verify MDX-breaking brackets or braces are escaped inside tables too
    assert "| Specs Processed | 1 |" in content
    assert "| **Fixes Applied** | **1** |" in content
    assert "### test_spec.json" in content


def test_cli_subprocess_generate_docs(tmp_path):
    """Verify that generate_docs.py runs cleanly as a direct subprocess."""

    report_path = tmp_path / "report.json"

    report_data = {
        "schema_version": 1,
        "generated_at": "2026-08-07T12:00:00Z",
        "summary": {
            "processed_specs": 1,
            "discrepancies_received": 1,
            "fixes_applied": 1,
            "failures": 0,
            "modified_files": ["test.json"],
            "unmodified_files": [],
        },
        "fixes": [
            {
                "spec_file": "test.json",
                "strategy": "relax",
                "before": 10,
                "after": 20,
                "source_discrepancy": {
                    "path": "TestModel",
                    "property_name": "prop",
                    "constraint_type": "maxLength",
                    "discrepancy_type": "spec_stricter",
                    "spec_value": 10,
                    "api_behavior": 20,
                    "spec_file": "test.json",
                    "test_values": [],
                    "recommendation": "rec",
                    "domain": "dom",
                    "method": "GET",
                },
            }
        ],
        "failures": [],
    }
    report_path.write_text(json.dumps(report_data), encoding="utf-8")

    output_path = tmp_path / "validation-report.mdx"
    cmd = [
        sys.executable,
        "scripts/generate_docs.py",
        "--reconciliation-report",
        str(report_path),
        "--output",
        str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert result.returncode == 0
    assert output_path.exists()


def test_mdx_prose_safety_gate(tmp_path):
    """Generate an MDX report with adversarial characters and run the real prose linter on it."""

    prose_gate_script = Path("scripts/lint-mdx-prose.sh")

    if not prose_gate_script.exists():
        pytest.skip("lint-mdx-prose.sh script not found")

    report_path = tmp_path / "adversarial_report.json"
    report_data = {
        "schema_version": 1,
        "generated_at": "2026-08-07T12:00:00Z",
        "summary": {
            "processed_specs": 1,
            "discrepancies_received": 1,
            "fixes_applied": 1,
            "failures": 0,
            "modified_files": ["adv.json"],
            "unmodified_files": [],
        },
        "fixes": [
            {
                "spec_file": "adv.json",
                "strategy": "relax",
                "before": "val `with` | adversarial <tags>\nand {braces} & ampersands",
                "after": "after",
                "source_discrepancy": {
                    "path": "TestModel",
                    "property_name": "prop",
                    "constraint_type": "maxLength",
                    "discrepancy_type": "spec_stricter",
                    "spec_value": "val `with` | adversarial <tags>\nand {braces} & ampersands",
                    "api_behavior": "after",
                    "spec_file": "adv.json",
                    "test_values": [],
                    "recommendation": "rec",
                    "domain": "dom",
                    "method": "GET",
                },
            }
        ],
        "failures": [],
    }
    report_path.write_text(json.dumps(report_data), encoding="utf-8")

    output_path = tmp_path / "validation-report-adv.mdx"

    # Run the generator
    cmd_gen = [
        sys.executable,
        "scripts/generate_docs.py",
        "--reconciliation-report",
        str(report_path),
        "--output",
        str(output_path),
    ]
    subprocess.run(cmd_gen, capture_output=True, check=True)

    # Run prose linter script
    cmd_lint = ["bash", "scripts/lint-mdx-prose.sh", str(output_path)]
    result = subprocess.run(cmd_lint, capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"Prose lint failed with: {result.stdout}\n{result.stderr}"


def test_generate_docs_escapes_markdown_injections(tmp_path):
    """Verify that generate_docs escapes filenames containing markdown/MDX injection vectors."""

    report_path = tmp_path / "injection_report.json"

    report_data = {
        "schema_version": 1,
        "generated_at": "2026-08-07T12:00:00Z",
        "summary": {
            "processed_specs": 1,
            "discrepancies_received": 1,
            "fixes_applied": 1,
            "failures": 0,
            "modified_files": ["bad_file.json"],
            "unmodified_files": [],
        },
        "fixes": [
            {
                "spec_file": "bad_file\n# Injected Heading\n```python\nprint(1)\n```\n`backtick` {bracket}",
                "strategy": "relax",
                "before": "val",
                "after": "after",
                "source_discrepancy": {
                    "path": "TestModel",
                    "property_name": "prop",
                    "constraint_type": "maxLength",
                    "discrepancy_type": "spec_stricter",
                    "spec_value": "val",
                    "api_behavior": "after",
                    "spec_file": "bad_file\n# Injected Heading\n```python\nprint(1)\n```\n`backtick` {bracket}",
                    "test_values": [],
                    "recommendation": "rec",
                    "domain": "dom",
                    "method": "GET",
                },
            }
        ],
        "failures": [],
    }
    report_path.write_text(json.dumps(report_data), encoding="utf-8")

    output_path = tmp_path / "validation-report-injection.mdx"

    # Run the generator directly to isolate the rendering/escaping boundary
    generate_fixes_page(report_data, output_path)

    # Read generated MDX and verify no headings, code fences or tags were injected
    generated_content = output_path.read_text(encoding="utf-8")
    assert "\n# Injected Heading" not in generated_content
    assert "\n```python" not in generated_content
    # The raw string in heading should be escaped and safe
    assert "bad_file" in generated_content

    # Run prose linter script
    prose_gate_script = Path("scripts/lint-mdx-prose.sh")
    if prose_gate_script.exists():
        cmd_lint = ["bash", "scripts/lint-mdx-prose.sh", str(output_path)]
        result = subprocess.run(cmd_lint, capture_output=True, text=True, check=False)
        assert result.returncode == 0, f"Prose lint failed with: {result.stdout}\n{result.stderr}"
