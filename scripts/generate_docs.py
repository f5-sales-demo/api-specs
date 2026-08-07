#!/usr/bin/env python3
"""Generate documentation from reconciliation reports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import jsonschema
from rich.console import Console

console = Console()

FRONTMATTER = """---
title: Validation Report
description: F5 XC API spec validation and fix report
---
"""


def _check_duplicate_keys(ordered_pairs):
    d = {}
    for k, v in ordered_pairs:
        if k in d:
            raise ValueError(f"Duplicate key: {k}")
        d[k] = v
    return d


def _parse_constant(c):
    raise ValueError(f"Non-finite JSON: {c}")


def load_strict_json(report_path: Path) -> dict:
    """Load and strictly parse a JSON report file."""
    if not report_path.exists():
        console.print(f"[red]Report not found: {report_path}[/red]")
        sys.exit(1)
        
    try:
        with report_path.open() as f:
            data = json.load(
                f,
                object_pairs_hook=_check_duplicate_keys,
                parse_constant=_parse_constant
            )
    except json.JSONDecodeError as e:
        console.print(f"[red]Failed to parse report: {e}[/red]")
        sys.exit(1)
    except ValueError as e:
        console.print(f"[red]Invalid JSON report: {e}[/red]")
        sys.exit(1)

    schema_path = Path("config/reconciliation_report.schema.json")
    if schema_path.exists():
        with schema_path.open() as sf:
            schema = json.load(sf)
        try:
            jsonschema.validate(instance=data, schema=schema)
        except jsonschema.ValidationError as e:
            console.print(f"[red]Report fails schema validation: {e.message}[/red]")
            sys.exit(1)

    return data


def _format_value(value: object) -> str:
    """Format a value for display in MDX table, escaping HTML chars."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        json_str = json.dumps(value)
        if len(json_str) > 50:
            json_str = f"{json_str[:47]}..."
        value = json_str
    
    # MDX-safe escaping
    str_value = str(value)
    str_value = str_value.replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;")
    return f"`{str_value}`"


def generate_fixes_page(report: dict, output_path: Path) -> None:
    lines = [
        FRONTMATTER.strip(),
        "",
        "# F5 XC API Spec Fixes Applied",
        "",
        "Validated and reconciled F5 Distributed Cloud OpenAPI specifications.",
        "",
        f"*Generated: {report.get('generated_at', 'unknown')}*",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|--------|-------|",
    ]
    
    summary = report.get("summary", {})
    lines.extend([
        f"| Specs Processed | {summary.get('processed_specs', 0)} |",
        f"| Discrepancies Received | {summary.get('discrepancies_received', 0)} |",
        f"| **Fixes Applied** | **{summary.get('fixes_applied', 0)}** |",
        f"| Failures | {summary.get('failures', 0)} |",
        "",
    ])

    fixes = report.get("fixes", [])
    if fixes:
        lines.extend([
            "## Fixes Applied",
            "",
        ])
        fixes_by_file = {}
        for fix in fixes:
            fixes_by_file.setdefault(fix["spec_file"], []).append(fix)
            
        for filename, file_fixes in sorted(fixes_by_file.items()):
            lines.extend([
                f"### {filename}",
                "",
                "| Property | Constraint | Strategy | Before | After |",
                "|----------|------------|----------|--------|-------|",
            ])
            for fix in file_fixes:
                sd = fix.get("source_discrepancy", {})
                prop = sd.get("property_name", "-")
                constraint = sd.get("constraint_type", "-")
                strategy = fix.get("strategy", "-")
                before = _format_value(fix.get("before"))
                after = _format_value(fix.get("after"))
                lines.append(f"| `{prop}` | `{constraint}` | {strategy} | {before} | {after} |")
            lines.extend([
                "",
            ])

    failures = report.get("failures", [])
    if failures:
        lines.extend([
            "## Failures",
            "",
        ])
        failures_by_file = {}
        for failure in failures:
            failures_by_file.setdefault(failure["spec_file"], []).append(failure)
            
        for filename, file_failures in sorted(failures_by_file.items()):
            lines.extend([
                f"### {filename}",
                "",
                "| Stage | Error |",
                "|-------|-------|",
            ])
            for failure in file_failures:
                stage = failure.get("stage", "-")
                err = _format_value(failure.get("error", "-"))
                lines.append(f"| {stage} | {err} |")
            lines.extend([
                "",
            ])

    lines.extend([
        "---",
        "",
        "*This page is auto-generated by the [validation pipeline](https://github.com/f5-sales-demo/api-specs/actions).*",
        ""
    ])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        f.write("\n".join(lines))
    console.print(f"[green]Generated: {output_path}[/green]")


def main() -> int:
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
        default=Path("docs/01-validation-report.mdx"),
        help="Output path for documentation page",
    )
    args = parser.parse_args()

    console.print("[bold blue]Generating Documentation[/bold blue]")
    report = load_strict_json(args.reconciliation_report)
    generate_fixes_page(report, args.output)
    console.print("[green]Documentation generation complete[/green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
