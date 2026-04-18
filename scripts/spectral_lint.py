"""Spectral OAS3 linting adapter for the api-specs pipeline."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console

from .utils.constraint_validator import Discrepancy, DiscrepancyType

console = Console()

MISSING_RULES = frozenset(
    {
        "oas3-api-servers",
        "info-contact",
        "operation-tags",
        "oas3-parameter-description",
        "operation-description",
        "info-description",
    }
)

UNUSED_RULES = frozenset(
    {
        "oas3-unused-component",
    }
)


def map_violation_to_discrepancy(violation: dict[str, Any]) -> Discrepancy:
    """Convert a Spectral JSON violation to a Discrepancy object."""
    code = violation["code"]
    source = violation.get("source", "")
    filename = Path(source).name if source else ""
    path_parts = violation.get("path", [])
    property_name = ".".join(str(p) for p in path_parts)

    if code in MISSING_RULES:
        discrepancy_type = DiscrepancyType.SPECTRAL_MISSING
    elif code in UNUSED_RULES:
        discrepancy_type = DiscrepancyType.SPECTRAL_UNUSED
    else:
        discrepancy_type = DiscrepancyType.SPECTRAL_INVALID

    return Discrepancy(
        path=filename,
        property_name=property_name,
        constraint_type=f"spectral:{code}",
        discrepancy_type=discrepancy_type,
        spec_value=None,
        api_behavior=None,
        recommendation=violation.get("message", ""),
    )


class SpectralAdapter:
    """Run Spectral CLI and convert output to pipeline-compatible format."""

    def __init__(self, ruleset: str = ".spectral.yaml") -> None:
        """Initialize the Spectral adapter with a ruleset path."""
        self.ruleset = ruleset

    def run_lint(self, spec_dir: Path) -> list[dict[str, Any]]:
        """Run Spectral lint on all JSON specs in a directory."""
        spectral_bin = shutil.which("spectral")
        if spectral_bin is None:
            console.print("[yellow]Warning: spectral binary not found[/yellow]")
            return []

        spec_files = sorted(spec_dir.glob("*.json"))
        spec_files = [f for f in spec_files if f.name.startswith(".") is False]
        if len(spec_files) == 0:
            console.print(f"[yellow]No JSON spec files found in {spec_dir}[/yellow]")
            return []

        file_args = [str(f) for f in spec_files]
        cmd = [
            spectral_bin,
            "lint",
            *file_args,
            "-f",
            "json",
            "--ruleset",
            self.ruleset,
        ]

        console.print(f"[dim]Running Spectral on {len(spec_files)} specs...[/dim]")
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603

        if result.stdout.strip():
            violations: list[dict[str, Any]] = json.loads(result.stdout)
            return violations
        return []

    def write_report(
        self,
        violations: list[dict[str, Any]],
        output_path: Path,
    ) -> None:
        """Write violations as a pipeline-compatible report."""
        discrepancies = []
        for v in violations:
            d = map_violation_to_discrepancy(v)
            discrepancies.append(
                {
                    "path": d.path,
                    "property_name": d.property_name,
                    "constraint_type": d.constraint_type,
                    "discrepancy_type": d.discrepancy_type.value,
                    "spec_value": d.spec_value,
                    "api_behavior": d.api_behavior,
                    "recommendation": d.recommendation,
                }
            )

        report = {
            "source": "spectral",
            "total_violations": len(violations),
            "discrepancies": discrepancies,
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2))
        console.print(
            f"[green]Spectral report: {output_path} ({len(discrepancies)} issues)[/green]"
        )

    def check_gate(
        self,
        violations: list[dict[str, Any]],
        max_errors: int | None,
        max_warnings: int | None,
    ) -> bool:
        """Check if violations pass the quality gate. Returns True if passed."""
        error_count = sum(1 for v in violations if v.get("severity") == 0)
        warn_count = sum(1 for v in violations if v.get("severity") == 1)

        console.print(
            f"[dim]Gate check: {error_count} errors, {warn_count} warnings[/dim]"
        )

        if max_errors is not None and error_count > max_errors:
            console.print(
                f"[red]Gate FAILED: {error_count} errors exceeds max {max_errors}[/red]"
            )
            return False
        if max_warnings is not None and warn_count > max_warnings:
            console.print(
                f"[red]Gate FAILED: {warn_count} warnings exceeds max {max_warnings}[/red]"
            )
            return False

        console.print("[green]Gate PASSED[/green]")
        return True


def main() -> int:
    """Main entry point for Spectral linting command."""
    parser = argparse.ArgumentParser(description="Spectral OAS3 linting adapter")
    parser.add_argument(
        "--mode",
        choices=["discover", "gate"],
        default="discover",
        help="discover: pre-reconcile scan; gate: post-reconcile quality check",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/validation.yaml"),
        help="Configuration file path",
    )
    parser.add_argument(
        "--spec-dir",
        type=Path,
        default=None,
        help="Directory containing specs to lint",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output report path",
    )

    args = parser.parse_args()

    config: dict[str, Any] = {}
    if args.config.exists():
        with args.config.open() as f:
            config = yaml.safe_load(f) or {}

    spectral_config = config.get("spectral", {})
    if spectral_config.get("enabled", True) is False:
        console.print("[dim]Spectral linting disabled in config[/dim]")
        return 0

    ruleset = spectral_config.get("ruleset", ".spectral.yaml")
    adapter = SpectralAdapter(ruleset=ruleset)

    if args.mode == "discover":
        spec_dir = args.spec_dir or Path(
            config.get("download", {}).get("output_dir", "specs/original")
        )
        output = args.output or Path("reports/spectral_report.json")

        violations = adapter.run_lint(spec_dir)
        adapter.write_report(violations, output)

        console.print(
            f"[bold]Spectral discover: {len(violations)} violations found[/bold]"
        )
        return 0

    # gate mode
    spec_dir = args.spec_dir or Path("release/specs")
    output = args.output or Path("reports/spectral_gate_report.json")
    gate_config = spectral_config.get("gate", {})
    max_errors = gate_config.get("max_errors")
    max_warnings = gate_config.get("max_warnings")

    violations = adapter.run_lint(spec_dir)
    adapter.write_report(violations, output)

    if adapter.check_gate(violations, max_errors, max_warnings) is False:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
