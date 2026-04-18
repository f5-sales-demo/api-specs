"""Spec reconciliation engine - fix discrepancies between spec and API."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from openapi_spec_validator import validate
from rich.console import Console

from .utils.constraint_validator import Discrepancy, DiscrepancyType
from .utils.spec_loader import save_spec_to_file

console = Console()

# Minimum parts for dotted property paths like "paths.{path}.{method}"
_MIN_PATH_PARTS = 3

# Index of the tag segment in URL path segments (e.g., /api/namespace → "namespace")
_TAG_SEGMENT_INDEX = 1


@dataclass
class ReconciliationResult:
    """Result of reconciling a single spec file."""

    filename: str
    original_path: Path
    modified: bool
    changes: list[dict] = field(default_factory=list)
    fixed_spec: dict | None = None
    validation_errors: list[str] = field(default_factory=list)


@dataclass
class ReconciliationConfig:
    """Configuration for spec reconciliation."""

    priority: list[str] = field(
        default_factory=lambda: ["existing", "discovery", "inferred"]
    )
    fix_strategies: dict[str, str] = field(
        default_factory=lambda: {
            "tighter_spec": "relax",
            "looser_spec": "tighten",
            "missing_constraint": "add",
            "extra_constraint": "remove",
        }
    )


class SpecReconciler:
    """Reconcile OpenAPI specs with discovered API behavior."""

    def __init__(
        self,
        original_dir: Path,
        output_dir: Path,
        config: ReconciliationConfig | None = None,
        spectral_config: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the spec reconciler with paths and configuration."""
        self.original_dir = Path(original_dir)
        self.output_dir = Path(output_dir)
        self.config = config or ReconciliationConfig()
        self.spectral_config = spectral_config or {}
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
        grouped: dict[str, list[Discrepancy]] = {}

        for d in discrepancies:
            # Extract filename from path
            filename = d.path.split(":")[0] if ":" in d.path else d.path

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
            with spec_path.open() as f:
                if spec_path.suffix == ".yaml":
                    original = yaml.safe_load(f)
                else:
                    original = json.load(f)
        except (json.JSONDecodeError, yaml.YAMLError, OSError) as e:
            console.print(f"[red]Failed to load {spec_path}: {e}[/red]")
            result.validation_errors.append(str(e))
            return result

        # If no discrepancies, pass through original
        if not discrepancies:
            result.modified = False
            result.fixed_spec = original
            console.print(
                f"[green]{spec_path.name}: No changes needed (pass-through)[/green]"
            )
            return result

        # Apply fixes
        fixed = copy.deepcopy(original)

        for discrepancy in discrepancies:
            change = self._apply_fix(fixed, discrepancy)
            if change:
                result.changes.append(change)
                result.modified = True

        # Always ensure security metadata is present
        if "security" not in fixed and self.spectral_config.get("security_scheme"):
            security_discrepancy = Discrepancy(
                path=spec_path.name,
                property_name="",
                constraint_type="spectral:checkov-security",
                discrepancy_type=DiscrepancyType.SPECTRAL_MISSING,
                spec_value=None,
                api_behavior=None,
            )
            security_result = self._apply_spectral_fix(fixed, security_discrepancy)
            if security_result is not None:
                result.changes.append(
                    {
                        "action": "add_security_schemes",
                        "constraint_type": "spectral:checkov-security",
                    }
                )
                result.modified = True

        # Validate fixed spec
        if result.modified:
            try:
                validate(fixed)
                result.fixed_spec = fixed
                console.print(
                    f"[yellow]{spec_path.name}: {len(result.changes)} fixes applied[/yellow]"
                )
            except Exception as e:  # pylint: disable=broad-exception-caught
                result.validation_errors.append(str(e))
                console.print(f"[red]{spec_path.name}: Fixed spec invalid: {e}[/red]")
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
    ) -> dict | None:
        """Apply a fix for a single discrepancy."""
        fix_strategy = self._get_fix_strategy(discrepancy)

        if fix_strategy == "spectral":
            return self._apply_spectral_fix(spec, discrepancy)
        if fix_strategy == "relax":
            return self._relax_constraint(spec, discrepancy)
        if fix_strategy == "tighten":
            return self._tighten_constraint(spec, discrepancy)
        if fix_strategy == "add":
            return self._add_constraint(spec, discrepancy)
        if fix_strategy == "remove":
            return self._remove_constraint(spec, discrepancy)

        return None

    def _get_fix_strategy(self, discrepancy: Discrepancy) -> str:
        """Determine fix strategy based on discrepancy type."""
        if discrepancy.constraint_type.startswith("spectral:"):
            return "spectral"

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
    ) -> dict | None:
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
    ) -> dict | None:
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
    ) -> dict | None:
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
    ) -> dict | None:
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

    def _apply_spectral_fix(
        self,
        spec: dict,
        discrepancy: Discrepancy,
    ) -> dict | None:
        """Route Spectral discrepancy to the appropriate fix method."""
        spectral_fixers = {
            "spectral:oas3-api-servers": self._add_servers,
            "spectral:info-contact": self._add_contact,
            "spectral:operation-tags": self._add_tags,
            "spectral:oas3-unused-component": self._remove_unused_component,
            "spectral:operation-operationId-unique": self._deduplicate_operation_id,
            "spectral:oas3-valid-schema-example": self._fix_schema_example,
            "spectral:no-script-tags-in-markdown": self._strip_script_tags,
            "spectral:checkov-security": self._add_security_schemes,
        }
        fixer = spectral_fixers.get(discrepancy.constraint_type)
        if fixer:
            return fixer(spec, discrepancy)
        return None

    def _add_servers(self, spec: dict, discrepancy: Discrepancy) -> dict | None:
        """Add servers block from spectral config."""
        servers = self.spectral_config.get("servers")
        if servers is None:
            return None
        spec["servers"] = copy.deepcopy(servers)
        return spec

    def _add_contact(self, spec: dict, discrepancy: Discrepancy) -> dict | None:
        """Add contact info to spec.info."""
        contact = self.spectral_config.get("contact")
        if contact is None:
            return None
        spec.setdefault("info", {})["contact"] = copy.deepcopy(contact)
        return spec

    def _add_security_schemes(
        self,
        spec: dict,
        discrepancy: Discrepancy,
    ) -> dict | None:
        """Add security scheme metadata for F5 XC API authentication."""
        security_config = self.spectral_config.get("security_scheme")
        if security_config is None:
            return None

        scheme_name = "apiKeyAuth"
        spec.setdefault("components", {}).setdefault("securitySchemes", {})[
            scheme_name
        ] = {
            "type": security_config.get("type", "apiKey"),
            "in": security_config.get("in", "header"),
            "name": security_config.get("name", "Authorization"),
            "description": security_config.get(
                "description", "F5 XC API Token (format: APIToken <token>)"
            ),
        }
        spec.setdefault("security", [{"apiKeyAuth": []}])
        return spec

    def _add_tags(self, spec: dict, discrepancy: Discrepancy) -> dict | None:
        """Derive and add tags from the API path prefix."""
        parts = discrepancy.property_name.split(".")
        if len(parts) < _MIN_PATH_PARTS or parts[0] != "paths":
            return None

        path_key = parts[1]
        method = parts[2]

        path_obj = spec.get("paths", {}).get(path_key, {})
        operation = path_obj.get(method)
        if operation is None:
            return None

        segments = [s for s in path_key.split("/") if s and s.startswith("{") is False]
        tag = "default"
        if len(segments) > _TAG_SEGMENT_INDEX:
            tag = segments[_TAG_SEGMENT_INDEX]
        elif segments:
            tag = segments[0]

        operation["tags"] = [tag]

        existing_tags = spec.setdefault("tags", [])
        if not any(t.get("name") == tag for t in existing_tags):
            existing_tags.append({"name": tag})

        return spec

    def _remove_unused_component(
        self, spec: dict, discrepancy: Discrepancy
    ) -> dict | None:
        """Remove an unused component schema."""
        parts = discrepancy.property_name.split(".")
        if (
            len(parts) < _MIN_PATH_PARTS
            or parts[0] != "components"
            or parts[1] != "schemas"
        ):
            return None

        schema_name = parts[2]
        schemas = spec.get("components", {}).get("schemas", {})
        if schema_name in schemas:
            del schemas[schema_name]
            return spec
        return None

    def _deduplicate_operation_id(
        self, spec: dict, discrepancy: Discrepancy
    ) -> dict | None:
        """Append HTTP method suffix to duplicate operationIds."""
        parts = discrepancy.property_name.split(".")
        if len(parts) < _MIN_PATH_PARTS or parts[0] != "paths":
            return None

        path_key = parts[1]
        method = parts[2]

        operation = spec.get("paths", {}).get(path_key, {}).get(method)
        if operation is None or "operationId" not in operation:
            return None

        operation["operationId"] = f"{operation['operationId']}_{method}"
        return spec

    def _fix_schema_example(self, spec: dict, discrepancy: Discrepancy) -> dict | None:
        """Remove invalid default/example values from schemas."""
        parts = discrepancy.property_name.split(".")
        if len(parts) < _MIN_PATH_PARTS:
            return None

        target_key = parts[-1]
        if target_key not in ("default", "example"):
            return None

        obj = spec
        for part in parts[:-1]:
            obj = obj.get(part, {})
            if isinstance(obj, dict) is False:
                return None

        if target_key in obj:
            del obj[target_key]
            return spec
        return None

    def _strip_script_tags(self, spec: dict, discrepancy: Discrepancy) -> dict | None:
        """Strip script tags from description fields."""
        parts = discrepancy.property_name.split(".")
        if len(parts) == 0 or parts[-1] != "description":
            return None

        obj = spec
        for part in parts[:-1]:
            obj = obj.get(part, {})
            if isinstance(obj, dict) is False:
                return None

        if "description" in obj and isinstance(obj["description"], str):
            obj["description"] = re.sub(
                r"<script[^>]*>.*?</script>",
                "",
                obj["description"],
                flags=re.DOTALL | re.IGNORECASE,
            ).strip()
            return spec
        return None

    def _find_schema(
        self,
        spec: dict,
        property_path: str,
    ) -> dict | None:
        """Find schema definition for a property path."""
        # Try components/schemas first
        components: dict = spec.get("components", {})
        schemas: dict = components.get("schemas", {})

        # Simple lookup by name
        if property_path in schemas:
            schema_val: dict = schemas[property_path]
            return schema_val

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
        if constraint_type == "minLength" and isinstance(api_behavior, int):
            # Lower the minimum
            return min(old_value or 0, api_behavior)
        if constraint_type == "maxLength" and isinstance(api_behavior, int):
            # Raise the maximum
            return max(old_value or 0, api_behavior)
        if constraint_type == "minimum" and isinstance(api_behavior, (int, float)):
            return min(old_value or 0, api_behavior)
        if constraint_type == "maximum" and isinstance(api_behavior, (int, float)):
            return max(old_value or 0, api_behavior)
        if (
            constraint_type == "enum"
            and isinstance(api_behavior, list)
            and isinstance(old_value, list)
        ):
            # Add missing enum values
            return list(set(old_value) | set(api_behavior))

        return api_behavior

    def _calculate_tightened_value(
        self,
        constraint_type: str,
        old_value: Any,
        api_behavior: Any,
    ) -> Any:
        """Calculate a tightened constraint value."""
        if constraint_type == "minLength" and isinstance(api_behavior, int):
            # Raise the minimum
            return max(old_value or 0, api_behavior)
        if constraint_type == "maxLength" and isinstance(api_behavior, int):
            # Lower the maximum
            return min(old_value or float("inf"), api_behavior)
        if constraint_type == "minimum" and isinstance(api_behavior, (int, float)):
            return max(old_value or float("-inf"), api_behavior)
        if constraint_type == "maximum" and isinstance(api_behavior, (int, float)):
            return min(old_value or float("inf"), api_behavior)
        if constraint_type == "enum" and isinstance(api_behavior, list):
            # Restrict to only observed enum values
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
            "changes_by_file": {r.filename: r.changes for r in modified},
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
            lines.extend(
                [
                    f"### {result.filename}",
                    "",
                ]
            )

            has_items = False
            for change in result.changes:
                if not isinstance(change, dict):
                    continue
                action = change.get("action", "")
                constraint = change.get("constraint", "")
                constraint_type = change.get("constraint_type", "")
                prop = change.get("property", "")
                old_val = change.get("old_value", "")
                new_val = change.get("new_value", "")

                if action == "relax":
                    lines.append(
                        f"- **Relaxed** `{constraint}` on `{prop}`: `{old_val}` → `{new_val}`"
                    )
                    has_items = True
                elif action == "tighten":
                    lines.append(
                        f"- **Tightened** `{constraint}` on `{prop}`: `{old_val}` → `{new_val}`"
                    )
                    has_items = True
                elif action == "add":
                    lines.append(f"- **Added** `{constraint}` to `{prop}`: `{new_val}`")
                    has_items = True
                elif action == "remove":
                    lines.append(
                        f"- **Removed** `{constraint}` from `{prop}` (was `{old_val}`)"
                    )
                    has_items = True
                elif constraint_type.startswith("spectral:"):
                    lines.append(
                        f"- **Spectral fix** `{constraint_type}`: {action or 'applied'}"
                    )
                    has_items = True

            if not has_items:
                lines.append(f"- *{len(result.changes)} Spectral fixes applied*")

            lines.append("")

        return "\n".join(lines)


def load_discrepancies(report_path: Path) -> list[Discrepancy]:
    """Load discrepancies from a validation report."""
    if not report_path.exists():
        return []

    with report_path.open() as f:
        report = json.load(f)

    return [
        Discrepancy(
            path=d.get("path", ""),
            property_name=d.get("property_name", ""),
            constraint_type=d.get("constraint_type", ""),
            discrepancy_type=DiscrepancyType(
                d.get("discrepancy_type", "constraint_mismatch")
            ),
            spec_value=d.get("spec_value"),
            api_behavior=d.get("api_behavior"),
            test_values=d.get("test_values", []),
            recommendation=d.get("recommendation", ""),
        )
        for d in report.get("discrepancies", [])
    ]


def main() -> int:
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
        nargs="+",
        default=[Path("reports/validation_report.json")],
        help="Validation report(s) with discrepancies",
    )

    args = parser.parse_args()

    # Load configuration
    if args.config.exists():
        with args.config.open() as f:
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
    discrepancies = []
    for report_path in args.report:
        loaded = load_discrepancies(report_path)
        discrepancies.extend(loaded)
        console.print(
            f"[dim]Loaded {len(loaded)} discrepancies from {report_path}[/dim]"
        )

    # Create reconciler
    recon_config = ReconciliationConfig(
        priority=reconciliation_config.get(
            "priority", ["existing", "discovery", "inferred"]
        ),
        fix_strategies=reconciliation_config.get("fix_strategies", {}),
    )

    reconciler = SpecReconciler(
        original_dir=original_dir,
        output_dir=output_dir,
        config=recon_config,
        spectral_config=config.get("spectral", {}),
    )

    # Run reconciliation
    reconciler.reconcile_all(discrepancies)

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
    console.print("\n[bold]Summary:[/bold]")
    console.print(f"  Modified: {len(summary['modified_files'])} files")
    console.print(f"  Unmodified: {len(summary['unmodified_files'])} files")
    console.print(f"  Total changes: {summary['total_changes']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
