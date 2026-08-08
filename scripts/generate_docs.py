#!/usr/bin/env python3
"""Generate documentation from reconciliation reports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from rich.console import Console

if __name__ == "__main__" and not __package__:
    # Allow running as direct script
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.utils.reconciliation_report import load_reconciliation_report

console = Console()

FRONTMATTER = """---
title: Validation Report
description: F5 XC API spec validation and fix report
---
"""


def _format_value(value: object) -> str:
    """Format a value for display in MDX table, escaping HTML chars, MDX braces, backticks, and newlines."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        str_value = "true" if value else "false"
    elif isinstance(value, (list, dict)):
        json_str = json.dumps(value)
        if len(json_str) > 50:
            json_str = f"{json_str[:47]}..."
        str_value = json_str
    else:
        str_value = str(value)

    if str_value == "-":
        return "-"

    # Centralized MDX-safe cell escaping
    escaped = str_value.replace("&", "&amp;")
    escaped = escaped.replace("<", "&lt;").replace(">", "&gt;")
    escaped = escaped.replace("|", "&#124;")
    escaped = escaped.replace("{", "&#123;").replace("}", "&#125;")
    escaped = escaped.replace("`", "&#96;")
    escaped = escaped.replace("\r\n", "<br />").replace("\n", "<br />")

    return f"`{escaped}`"


def _escape_markdown(text: str) -> str:
    """Escapes any Markdown/MDX special syntax characters to make it completely inert."""
    if not text:
        return ""
    # Replace markdown symbols with their safe representations
    escaped = text
    # Replace backslashes first
    escaped = escaped.replace("\\", "\\\\")
    # Escape other Markdown special characters
    special_chars = ["#", "*", "`", "[", "]", "(", ")", "{", "}", "<", ">", "|", "$", "+", "!", "="]
    for char in special_chars:
        escaped = escaped.replace(char, f"\\{char}")
    # Replace any newlines/carriage returns with space to prevent injecting headers on a new line
    escaped = escaped.replace("\r", " ").replace("\n", " ")
    return escaped


def _format_fixes_section(fixes: list[dict[str, Any]]) -> list[str]:
    """Format fixes section for the MDX report."""
    if not fixes:
        return []

    lines = [
        "## Fixes Applied",
        "",
    ]
    fixes_by_file: dict[str, list[dict[str, Any]]] = {}
    for fix in fixes:
        fixes_by_file.setdefault(fix["spec_file"], []).append(fix)

    for filename, file_fixes in sorted(fixes_by_file.items()):
        lines.extend(
            [
                f"### {_escape_markdown(filename)}",
                "",
                "| Property | Constraint | Discrepancy Type | Strategy | Before | After |",
                "| :--- | :--- | :--- | :--- | :--- | :--- |",
            ]
        )
        for fix in file_fixes:
            sd = fix.get("source_discrepancy", {})
            prop = _format_value(sd.get("property_name"))
            constraint = _format_value(sd.get("constraint_type"))
            dtype = _format_value(sd.get("discrepancy_type"))
            strategy = _format_value(fix.get("strategy"))
            before = _format_value(fix.get("before"))
            after = _format_value(fix.get("after"))
            lines.append(f"| {prop} | {constraint} | {dtype} | {strategy} | {before} | {after} |")
        lines.extend(
            [
                "",
            ]
        )
    return lines


def _format_failures_section(failures: list[dict[str, Any]]) -> list[str]:
    """Format failures section for the MDX report."""
    if not failures:
        return []

    lines = [
        "## Failures",
        "",
    ]
    failures_by_file: dict[str, list[dict[str, Any]]] = {}
    for failure in failures:
        failures_by_file.setdefault(failure["spec_file"], []).append(failure)

    for filename, file_failures in sorted(failures_by_file.items()):
        lines.extend(
            [
                f"### {_escape_markdown(filename)}",
                "",
                "| Property | Constraint | Discrepancy Type | Stage | Error |",
                "| :--- | :--- | :--- | :--- | :--- |",
            ]
        )
        for failure in file_failures:
            sd = failure.get("source_discrepancy", {})
            prop = _format_value(sd.get("property_name"))
            constraint = _format_value(sd.get("constraint_type"))
            dtype = _format_value(sd.get("discrepancy_type"))
            stage = _format_value(failure.get("stage"))
            err = _format_value(failure.get("error"))
            lines.append(f"| {prop} | {constraint} | {dtype} | {stage} | {err} |")
        lines.extend(
            [
                "",
            ]
        )
    return lines


def generate_fixes_page(report: dict, output_path: Path) -> None:
    """Generate MDX documentation from reconciliation report."""
    lines = [
        FRONTMATTER.strip(),
        "",
        "## F5 XC API Spec Fixes Applied",
        "",
        "Validated and reconciled F5 Distributed Cloud OpenAPI specifications.",
        "",
        f"Generated: {report.get('generated_at', 'unknown')}",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "| :--- | :--- |",
    ]

    summary = report.get("summary", {})
    lines.extend(
        [
            f"| Specs Processed | {summary.get('processed_specs', 0)} |",
            f"| Discrepancies Received | {summary.get('discrepancies_received', 0)} |",
            f"| **Fixes Applied** | **{summary.get('fixes_applied', 0)}** |",
            f"| Failures | {summary.get('failures', 0)} |",
            "",
        ]
    )

    lines.extend(_format_fixes_section(report.get("fixes", [])))
    lines.extend(_format_failures_section(report.get("failures", [])))

    lines.extend(
        [
            "---",
            "",
            "*This page is auto-generated by the [validation pipeline](https://github.com/f5-sales-demo/api-specs/actions).*",
            "",
        ]
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        f.write("\n".join(lines))
    console.print(f"[green]Generated: {output_path}[/green]")


def main() -> int:
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
    args = parser.parse_args()

    console.print("[bold blue]Generating Documentation[/bold blue]")
    try:
        report = load_reconciliation_report(args.reconciliation_report)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
        console.print(f"[red]Failed to load reconciliation report: {e}[/red]")
        return 1

    generate_fixes_page(report, args.output)
    console.print("[green]Documentation generation complete[/green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
