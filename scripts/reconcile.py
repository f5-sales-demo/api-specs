"""Spec reconciliation engine - fix discrepancies between spec and API."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from deepdiff import DeepDiff
from openapi_spec_validator import validate
from rich.console import Console

from .utils.constraint_validator import Discrepancy, DiscrepancyType
from .utils.spec_loader import SpecLoader, save_spec_to_file

console = Console()


@dataclass
class ReconciliationResult:
    """Result of reconciling a single spec file."""

    filename: str
    original_path: Path
    modified: bool
    changes: list[dict] = field(default_factory=list)
    fixed_spec: Optional[dict] = None
    validation_errors: list[str] = field(default_factory=list)


@dataclass
class ReconciliationConfig:
    """Configuration for spec reconciliation."""

    priority: list[str] = field(
        default_factory=lambda: ["existing", "discovery", "inferred"]
    )
    fix_strategies: dict[str, str] = field(default_factory=lambda: {
        "tighter_spec": "relax",
        "looser_spec": "tighten",
        "missing_constraint": "add",
        "extra_constraint": "remove",
    })


class SpecReconciler:
    """Reconcile OpenAPI specs with discovered API behavior."""

    def __init__(
        self,
        original_dir: Path,
        output_dir: Path,
        config: Optional[ReconciliationConfig] = None,
    ):
        self.original_dir = Path(original_dir)
        self.output_dir = Path(output_dir)
        self.config = config or ReconciliationConfig()
        self.results: list[ReconciliationResult] = []

        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def reconcile_all(
        self,
        discrepancies: list[Discrepancy],
    ) -> list[ReconciliationResult]:
        """Reconcile all specs based on discovered discrepancies."""
        console.print("[bold blue]Reconciling Specs[/bold blue]")

        # Group discrepancies by file
        discrepancies_by_file = self._group_by_file(discrepancies)

        # Process each original spec file
        for spec_file in self.original_dir.glob("*.json"):
            result = self._reconcile_file(
                spec_file,
                discrepancies_by_file.get(spec_file.name, []),
            )
            self.results.append(result)

        # Also handle YAML files if present
        for spec_file in self.original_dir.glob("*.yaml"):
            result = self._reconcile_file(
                spec_file,
                discrepancies_by_file.get(spec_file.name, []),
            )
            self.results.append(result)

        return self.results

    def _group_by_file(
        self,
        discrepancies: list[Discrepancy],
    ) -> dict[str, list[Discrepancy]]:
        """Group discrepancies by source file."""
        grouped = {}

        for d in discrepancies:
            # Extract filename from path
            if ":" in d.path:
                filename = d.path.split(":")[0]
            else:
                filename = d.path

            if filename not in grouped:
                grouped[filename] = []
            grouped[filename].append(d)

        return grouped

    def _reconcile_file(
        self,
        spec_path: Path,
        discrepancies: list[Discrepancy],
    ) -> ReconciliationResult:
        """Reconcile a single spec file."""
        result = ReconciliationResult(
            filename=spec_path.name,
            original_path=spec_path,
            modified=False,
        )

        # Load original spec
        try:
            with open(spec_path) as f:
                if spec_path.suffix == ".yaml":
                    original = yaml.safe_load(f)
                else:
                    original = json.load(f)
        except Exception as e:
            console.print(f"[red]Failed to load {spec_path}: {e}[/red]")
            result.validation_errors.append(str(e))
            return result

        # If no discrepancies, pass through original
        if not discrepancies:
            result.modified = False
            result.fixed_spec = original
            console.print(f"[green]{spec_path.name}: No changes needed (pass-through)[/green]")
            return result

        # Apply fixes
        fixed = copy.deepcopy(original)

        for discrepancy in discrepancies:
            change = self._apply_fix(fixed, discrepancy)
            if change:
                result.changes.append(change)
                result.modified = True

        # Validate fixed spec
        if result.modified:
            try:
                validate(fixed)
                result.fixed_spec = fixed
                console.print(
                    f"[yellow]{spec_path.name}: {len(result.changes)} fixes applied[/yellow]"
                )
            except Exception as e:
                result.validation_errors.append(str(e))
                console.print(
                    f"[red]{spec_path.name}: Fixed spec invalid: {e}[/red]"
                )
                # Fall back to original
                result.fixed_spec = original
                result.modified = False
        else:
            result.fixed_spec = original

        return result

    def _apply_fix(
        self,
        spec: dict,
        discrepancy: Discrepancy,
    ) -> Optional[dict]:
        """Apply a fix for a single discrepancy."""
        fix_strategy = self._get_fix_strategy(discrepancy)

        if fix_strategy == "relax":
            return self._relax_constraint(spec, discrepancy)
        elif fix_strategy == "tighten":
            return self._tighten_constraint(spec, discrepancy)
        elif fix_strategy == "add":
            return self._add_constraint(spec, discrepancy)
        elif fix_strategy == "remove":
            return self._remove_constraint(spec, discrepancy)

        return None

    def _get_fix_strategy(self, discrepancy: Discrepancy) -> str:
        """Determine fix strategy based on discrepancy type."""
        strategy_map = {
            DiscrepancyType.SPEC_STRICTER: self.config.fix_strategies.get(
                "tighter_spec", "relax"
            ),
            DiscrepancyType.SPEC_LOOSER: self.config.fix_strategies.get(
                "looser_spec", "tighten"
            ),
            DiscrepancyType.MISSING_CONSTRAINT: self.config.fix_strategies.get(
                "missing_constraint", "add"
            ),
            DiscrepancyType.EXTRA_CONSTRAINT: self.config.fix_strategies.get(
                "extra_constraint", "remove"
            ),
        }
        return strategy_map.get(discrepancy.discrepancy_type, "skip")

    def _relax_constraint(
        self,
        spec: dict,
        discrepancy: Discrepancy,
    ) -> Optional[dict]:
        """Relax a constraint that is too strict."""
        # Navigate to the constraint location
        schema = self._find_schema(spec, discrepancy.property_name)
        if not schema:
            return None

        constraint_type = discrepancy.constraint_type
        old_value = schema.get(constraint_type)

        # Determine new relaxed value based on API behavior
        new_value = self._calculate_relaxed_value(
            constraint_type,
            old_value,
            discrepancy.api_behavior,
        )

        if new_value is not None and new_value != old_value:
            schema[constraint_type] = new_value
            return {
                "action": "relax",
                "path": discrepancy.path,
                "property": discrepancy.property_name,
                "constraint": constraint_type,
                "old_value": old_value,
                "new_value": new_value,
            }

        return None

    def _tighten_constraint(
        self,
        spec: dict,
        discrepancy: Discrepancy,
    ) -> Optional[dict]:
        """Tighten a constraint that is too loose."""
        schema = self._find_schema(spec, discrepancy.property_name)
        if not schema:
            return None

        constraint_type = discrepancy.constraint_type
        old_value = schema.get(constraint_type)

        # Determine new tightened value based on API behavior
        new_value = self._calculate_tightened_value(
            constraint_type,
            old_value,
            discrepancy.api_behavior,
        )

        if new_value is not None and new_value != old_value:
            schema[constraint_type] = new_value
            return {
                "action": "tighten",
                "path": discrepancy.path,
                "property": discrepancy.property_name,
                "constraint": constraint_type,
                "old_value": old_value,
                "new_value": new_value,
            }

        return None

    def _add_constraint(
        self,
        spec: dict,
        discrepancy: Discrepancy,
    ) -> Optional[dict]:
        """Add a missing constraint."""
        schema = self._find_schema(spec, discrepancy.property_name)
        if not schema:
            return None

        constraint_type = discrepancy.constraint_type
        new_value = discrepancy.api_behavior

        if constraint_type not in schema:
            schema[constraint_type] = new_value
            return {
                "action": "add",
                "path": discrepancy.path,
                "property": discrepancy.property_name,
                "constraint": constraint_type,
                "new_value": new_value,
            }

        return None

    def _remove_constraint(
        self,
        spec: dict,
        discrepancy: Discrepancy,
    ) -> Optional[dict]:
        """Remove an extra constraint that API ignores."""
        schema = self._find_schema(spec, discrepancy.property_name)
        if not schema:
            return None

        constraint_type = discrepancy.constraint_type

        if constraint_type in schema:
            old_value = schema.pop(constraint_type)
            return {
                "action": "remove",
                "path": discrepancy.path,
                "property": discrepancy.property_name,
                "constraint": constraint_type,
                "old_value": old_value,
            }

        return None

    def _find_schema(
        self,
        spec: dict,
        property_path: str,
    ) -> Optional[dict]:
        """Find schema definition for a property path."""
        # Try components/schemas first
        components = spec.get("components", {})
        schemas = components.get("schemas", {})

        # Simple lookup by name
        if property_path in schemas:
            return schemas[property_path]

        # Try nested path
        parts = property_path.split("/")
        current = schemas
        for part in parts:
            if isinstance(current, dict):
                if part in current:
                    current = current[part]
                elif "properties" in current and part in current["properties"]:
                    current = current["properties"][part]
                else:
                    return None
            else:
                return None

        return current if isinstance(current, dict) else None

    def _calculate_relaxed_value(
        self,
        constraint_type: str,
        old_value: Any,
        api_behavior: Any,
    ) -> Any:
        """Calculate a relaxed constraint value."""
        if constraint_type == "minLength":
            # Lower the minimum
            if isinstance(api_behavior, int):
                return min(old_value or 0, api_behavior)
        elif constraint_type == "maxLength":
            # Raise the maximum
            if isinstance(api_behavior, int):
                return max(old_value or 0, api_behavior)
        elif constraint_type == "minimum":
            if isinstance(api_behavior, (int, float)):
                return min(old_value or 0, api_behavior)
        elif constraint_type == "maximum":
            if isinstance(api_behavior, (int, float)):
                return max(old_value or 0, api_behavior)
        elif constraint_type == "enum":
            # Add missing enum values
            if isinstance(api_behavior, list) and isinstance(old_value, list):
                return list(set(old_value) | set(api_behavior))

        return api_behavior

    def _calculate_tightened_value(
        self,
        constraint_type: str,
        old_value: Any,
        api_behavior: Any,
    ) -> Any:
        """Calculate a tightened constraint value."""
        if constraint_type == "minLength":
            # Raise the minimum
            if isinstance(api_behavior, int):
                return max(old_value or 0, api_behavior)
        elif constraint_type == "maxLength":
            # Lower the maximum
            if isinstance(api_behavior, int):
                return min(old_value or float("inf"), api_behavior)
        elif constraint_type == "minimum":
            if isinstance(api_behavior, (int, float)):
                return max(old_value or float("-inf"), api_behavior)
        elif constraint_type == "maximum":
            if isinstance(api_behavior, (int, float)):
                return min(old_value or float("inf"), api_behavior)
        elif constraint_type == "enum":
            # Restrict to only observed enum values
            if isinstance(api_behavior, list):
                return api_behavior

        return api_behavior

    def save_results(self) -> dict[str, Path]:
        """Save reconciled specs to output directory."""
        saved_files = {}

        for result in self.results:
            if result.fixed_spec is None:
                continue

            # Determine output path
            output_path = self.output_dir / result.filename

            # Save in original format
            if result.filename.endswith(".yaml"):
                save_spec_to_file(result.fixed_spec, output_path, "yaml")
            else:
                save_spec_to_file(result.fixed_spec, output_path, "json")

            saved_files[result.filename] = output_path

            status = "fixed" if result.modified else "pass-through"
            console.print(f"  [dim]Saved: {result.filename} ({status})[/dim]")

        return saved_files

    def get_summary(self) -> dict:
        """Get reconciliation summary."""
        modified = [r for r in self.results if r.modified]
        unmodified = [r for r in self.results if not r.modified]

        total_changes = sum(len(r.changes) for r in modified)

        return {
            "total_files": len(self.results),
            "modified_files": [r.filename for r in modified],
            "unmodified_files": [r.filename for r in unmodified],
            "total_changes": total_changes,
            "changes_by_file": {
                r.filename: r.changes for r in modified
            },
        }

    def generate_changelog(self) -> str:
        """Generate changelog of all modifications."""
        lines = [
            "# Changelog",
            "",
            "## Spec Modifications",
            "",
        ]

        modified = [r for r in self.results if r.modified]

        if not modified:
            lines.append("*No modifications were required.*")
            return "\n".join(lines)

        for result in modified:
            lines.extend([
                f"### {result.filename}",
                "",
            ])

            for change in result.changes:
                action = change.get("action", "unknown")
                constraint = change.get("constraint", "")
                prop = change.get("property", "")
                old_val = change.get("old_value", "")
                new_val = change.get("new_value", "")

                if action == "relax":
                    lines.append(
                        f"- **Relaxed** `{constraint}` on `{prop}`: "
                        f"`{old_val}` → `{new_val}`"
                    )
                elif action == "tighten":
                    lines.append(
                        f"- **Tightened** `{constraint}` on `{prop}`: "
                        f"`{old_val}` → `{new_val}`"
                    )
                elif action == "add":
                    lines.append(
                        f"- **Added** `{constraint}` to `{prop}`: `{new_val}`"
                    )
                elif action == "remove":
                    lines.append(
                        f"- **Removed** `{constraint}` from `{prop}` "
                        f"(was `{old_val}`)"
                    )

            lines.append("")

        return "\n".join(lines)


def load_discrepancies(report_path: Path) -> list[Discrepancy]:
    """Load discrepancies from a validation report."""
    if not report_path.exists():
        return []

    with open(report_path) as f:
        report = json.load(f)

    discrepancies = []
    for d in report.get("discrepancies", []):
        discrepancies.append(Discrepancy(
            path=d.get("path", ""),
            property_name=d.get("property_name", ""),
            constraint_type=d.get("constraint_type", ""),
            discrepancy_type=DiscrepancyType(d.get("discrepancy_type", "constraint_mismatch")),
            spec_value=d.get("spec_value"),
            api_behavior=d.get("api_behavior"),
            test_values=d.get("test_values", []),
            recommendation=d.get("recommendation", ""),
        ))

    return discrepancies


def main():
    """Main entry point for reconciliation command."""
    parser = argparse.ArgumentParser(
        description="Reconcile F5 XC OpenAPI specs with API behavior"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/validation.yaml"),
        help="Configuration file path",
    )
    parser.add_argument(
        "--original-dir",
        type=Path,
        default=None,
        help="Directory containing original specs",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for reconciled specs",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/validation_report.json"),
        help="Validation report with discrepancies",
    )

    args = parser.parse_args()

    # Load configuration
    if args.config.exists():
        with open(args.config) as f:
            config = yaml.safe_load(f)
    else:
        config = {}

    # Determine paths
    download_config = config.get("download", {})
    reconciliation_config = config.get("reconciliation", {})

    original_dir = args.original_dir or Path(
        download_config.get("output_dir", "specs/original")
    )
    output_dir = args.output_dir or Path("release/specs")

    # Load discrepancies from report
    discrepancies = load_discrepancies(args.report)
    console.print(f"[dim]Loaded {len(discrepancies)} discrepancies from report[/dim]")

    # Create reconciler
    recon_config = ReconciliationConfig(
        priority=reconciliation_config.get("priority", ["existing", "discovery", "inferred"]),
        fix_strategies=reconciliation_config.get("fix_strategies", {}),
    )

    reconciler = SpecReconciler(
        original_dir=original_dir,
        output_dir=output_dir,
        config=recon_config,
    )

    # Run reconciliation
    results = reconciler.reconcile_all(discrepancies)

    # Save results
    saved = reconciler.save_results()
    console.print(f"\n[green]Saved {len(saved)} spec files[/green]")

    # Generate and save changelog
    changelog = reconciler.generate_changelog()
    changelog_path = output_dir / "CHANGELOG.md"
    changelog_path.write_text(changelog)
    console.print(f"[green]Changelog: {changelog_path}[/green]")

    # Print summary
    summary = reconciler.get_summary()
    console.print(f"\n[bold]Summary:[/bold]")
    console.print(f"  Modified: {len(summary['modified_files'])} files")
    console.print(f"  Unmodified: {len(summary['unmodified_files'])} files")
    console.print(f"  Total changes: {summary['total_changes']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
