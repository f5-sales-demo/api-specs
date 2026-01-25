"""Validation orchestrator for F5 XC API specs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import yaml
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from .utils.auth import F5XCAuth, load_auth_from_config
from .utils.spec_loader import SpecLoader
from .utils.constraint_validator import (
    ConstraintValidator,
    Discrepancy,
    DiscrepancyType,
    ValidationTestCase,
)
from .utils.schemathesis_runner import SchemathesisRunner, create_runner
from .utils.report_generator import ReportGenerator, create_report_generator

console = Console()


def load_config(config_path: Path) -> dict:
    """Load configuration from YAML file."""
    if not config_path.exists():
        console.print(f"[red]Config file not found: {config_path}[/red]")
        sys.exit(1)

    with open(config_path) as f:
        return yaml.safe_load(f)


def load_endpoints_config(config_path: Path) -> dict:
    """Load endpoints configuration."""
    if not config_path.exists():
        console.print(f"[red]Endpoints config not found: {config_path}[/red]")
        sys.exit(1)

    with open(config_path) as f:
        return yaml.safe_load(f)


class ValidationOrchestrator:
    """Orchestrate validation of F5 XC API specs."""

    def __init__(
        self,
        config: dict,
        endpoints_config: dict,
        auth: Optional[F5XCAuth] = None,
        dry_run: bool = False,
    ):
        self.config = config
        self.endpoints_config = endpoints_config
        self.dry_run = dry_run

        # Initialize components
        self.spec_loader = SpecLoader(
            Path(config.get("download", {}).get("output_dir", "specs/original"))
        )
        self.constraint_validator = ConstraintValidator()

        # Auth and Schemathesis only if not dry run
        self.auth = auth
        self.schemathesis_runner: Optional[SchemathesisRunner] = None

        if not dry_run and auth:
            self.schemathesis_runner = create_runner(
                auth,
                config.get("schemathesis", {}),
            )

        # Report generator
        self.report_generator = create_report_generator(
            config.get("reports", {})
        )

        # Results storage
        self.discrepancies: list[Discrepancy] = []
        self.test_results = []

    def run(
        self,
        endpoint_filter: Optional[str] = None,
        schemathesis_only: bool = False,
    ) -> int:
        """Run the full validation pipeline."""
        console.print("[bold blue]F5 XC API Spec Validation[/bold blue]")

        if self.dry_run:
            console.print("[yellow]Running in dry-run mode (no live API calls)[/yellow]")

        # Step 1: Load and validate specs
        console.print("\n[bold]Step 1: Loading OpenAPI Specs[/bold]")
        specs = self._load_specs()

        if not specs:
            console.print("[red]No specs found. Run 'make download' first.[/red]")
            return 1

        # Step 2: Validate spec structure
        console.print("\n[bold]Step 2: Validating Spec Structure[/bold]")
        spec_errors = self._validate_spec_structure(specs)

        # Step 3: Extract constraints
        console.print("\n[bold]Step 3: Extracting Constraints[/bold]")
        constraints = self._extract_constraints(specs)

        # Step 4: Generate test cases
        console.print("\n[bold]Step 4: Generating Test Cases[/bold]")
        test_cases = self._generate_test_cases(constraints)

        if not self.dry_run:
            # Step 5: Run Schemathesis tests
            if self.schemathesis_runner:
                console.print("\n[bold]Step 5: Running Schemathesis Tests[/bold]")
                self._run_schemathesis_tests(specs, endpoint_filter)

            # Step 6: Run constraint validation tests
            if not schemathesis_only:
                console.print("\n[bold]Step 6: Running Constraint Validation[/bold]")
                self._run_constraint_tests(test_cases, endpoint_filter)
        else:
            console.print("\n[yellow]Skipping live API tests (dry run)[/yellow]")

        # Step 7: Generate reports
        console.print("\n[bold]Step 7: Generating Reports[/bold]")
        self._generate_reports()

        # Print summary
        self._print_summary()

        return 0 if not self.discrepancies else 1

    def _load_specs(self) -> dict[str, dict]:
        """Load all OpenAPI specs."""
        try:
            return self.spec_loader.load_all_domain_files()
        except Exception as e:
            console.print(f"[red]Failed to load specs: {e}[/red]")
            return {}

    def _validate_spec_structure(self, specs: dict[str, dict]) -> dict[str, list[str]]:
        """Validate structure of each spec."""
        errors = {}

        for filename, spec in specs.items():
            is_valid, spec_errors = self.spec_loader.validate_spec(spec)
            if not is_valid:
                errors[filename] = spec_errors
                console.print(f"[red]Invalid spec: {filename}[/red]")
                for error in spec_errors[:3]:
                    console.print(f"  [dim]{error}[/dim]")
            else:
                console.print(f"[green]Valid: {filename}[/green]")

        return errors

    def _extract_constraints(self, specs: dict[str, dict]) -> dict:
        """Extract constraints from all specs."""
        all_constraints = {}

        for filename, spec in specs.items():
            schemas = self.spec_loader.extract_schemas(spec)

            for schema_name, schema_info in schemas.items():
                if schema_info.constraints:
                    key = f"{filename}:{schema_name}"
                    all_constraints[key] = {
                        "schema": schema_info,
                        "constraints": schema_info.constraints,
                    }

            console.print(
                f"[dim]{filename}: {len(schemas)} schemas, "
                f"{sum(len(s.constraints) for s in schemas.values())} constraints[/dim]"
            )

        console.print(f"[green]Total: {len(all_constraints)} schemas with constraints[/green]")
        return all_constraints

    def _generate_test_cases(self, constraints: dict) -> dict[str, list[ValidationTestCase]]:
        """Generate test cases for all constraints."""
        all_test_cases = {}
        total_tests = 0

        for key, data in constraints.items():
            test_cases = []
            for constraint_type, constraint_value in data["constraints"].items():
                # Skip nested property constraints for now
                if "." in constraint_type:
                    continue

                cases = self.constraint_validator.generate_test_cases(
                    constraint_type,
                    constraint_value,
                    data["schema"].schema,
                )
                test_cases.extend(cases)

            if test_cases:
                all_test_cases[key] = test_cases
                total_tests += len(test_cases)

        console.print(f"[green]Generated {total_tests} test cases[/green]")
        return all_test_cases

    def _run_schemathesis_tests(
        self,
        specs: dict[str, dict],
        endpoint_filter: Optional[str] = None,
    ) -> None:
        """Run Schemathesis property-based tests."""
        if not self.schemathesis_runner:
            return

        # Get target endpoints from config
        endpoints_config = self.endpoints_config.get("endpoints", {})

        # Group specs by domain file
        domain_endpoints = {}
        for endpoint_name, endpoint_config in endpoints_config.items():
            domain_file = endpoint_config.get("domain_file")
            if domain_file not in domain_endpoints:
                domain_endpoints[domain_file] = []
            domain_endpoints[domain_file].append(endpoint_config)

        # Run tests for each domain
        for domain_file, endpoints in domain_endpoints.items():
            if domain_file not in specs:
                console.print(f"[yellow]Domain file not found: {domain_file}[/yellow]")
                continue

            console.print(f"\n[cyan]Testing {domain_file}[/cyan]")

            spec = specs[domain_file]
            schema = self.schemathesis_runner.load_schema(spec)

            for endpoint_config in endpoints:
                resource = endpoint_config.get("resource")

                if endpoint_filter and endpoint_filter not in resource:
                    continue

                console.print(f"  [dim]Testing: {resource}[/dim]")

                try:
                    results = self.schemathesis_runner.run_stateful_tests(
                        schema,
                        resource,
                    )
                    self.test_results.extend(results)

                    # Collect discrepancies
                    for result in results:
                        self.discrepancies.extend(result.discrepancies)

                except Exception as e:
                    console.print(f"  [red]Error testing {resource}: {e}[/red]")

    def _run_constraint_tests(
        self,
        test_cases: dict[str, list[ValidationTestCase]],
        endpoint_filter: Optional[str] = None,
    ) -> None:
        """Run constraint validation tests against live API."""
        if not self.auth:
            console.print("[yellow]No auth configured, skipping constraint tests[/yellow]")
            return

        endpoints_config = self.endpoints_config.get("endpoints", {})

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task(
                "Running constraint tests...",
                total=len(endpoints_config),
            )

            for endpoint_name, endpoint_config in endpoints_config.items():
                if endpoint_filter and endpoint_filter not in endpoint_name:
                    progress.update(task, advance=1)
                    continue

                resource = endpoint_config.get("resource")
                crud_ops = endpoint_config.get("crud_operations", {})

                # Test create operation
                if "create" in crud_ops:
                    self._test_endpoint_constraints(
                        endpoint_name,
                        crud_ops["create"],
                        test_cases,
                    )

                progress.update(task, advance=1)

    def _test_endpoint_constraints(
        self,
        endpoint_name: str,
        create_path: str,
        test_cases: dict[str, list[ValidationTestCase]],
    ) -> None:
        """Test constraints for a specific endpoint."""
        # This would be implemented to actually test constraints
        # For now, we just log what would be tested
        console.print(f"  [dim]Would test constraints for: {endpoint_name}[/dim]")

    def _generate_reports(self) -> None:
        """Generate validation reports."""
        # Determine modified vs unmodified files
        modified_files = []
        unmodified_files = []

        # For now, all files are considered unmodified until reconciliation
        specs = self.spec_loader.load_all_domain_files()
        for filename in specs.keys():
            if any(d.path and filename in d.path for d in self.discrepancies):
                modified_files.append(filename)
            else:
                unmodified_files.append(filename)

        # Generate reports
        self.report_generator.generate_all(
            results=self.test_results,
            discrepancies=self.discrepancies,
            modified_files=modified_files,
            unmodified_files=unmodified_files,
        )

    def _print_summary(self) -> None:
        """Print validation summary."""
        console.print("\n" + "=" * 60)
        console.print("[bold]Validation Summary[/bold]")
        console.print("=" * 60)

        console.print(f"Tests run: {len(self.test_results)}")
        console.print(f"Discrepancies found: {len(self.discrepancies)}")

        if self.discrepancies:
            # Group by type
            by_type = {}
            for d in self.discrepancies:
                dtype = d.discrepancy_type.value
                by_type[dtype] = by_type.get(dtype, 0) + 1

            console.print("\n[bold]Discrepancies by type:[/bold]")
            for dtype, count in sorted(by_type.items()):
                console.print(f"  {dtype}: {count}")

        if self.schemathesis_runner:
            summary = self.schemathesis_runner.get_summary()
            console.print(f"\n[bold]Schemathesis Summary:[/bold]")
            console.print(f"  Operations tested: {summary['total_operations']}")
            console.print(f"  Pass rate: {summary['pass_rate']:.1%}")


def main():
    """Main entry point for validation command."""
    parser = argparse.ArgumentParser(
        description="Validate F5 XC OpenAPI specs against live API"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/validation.yaml"),
        help="Main configuration file",
    )
    parser.add_argument(
        "--endpoints",
        type=Path,
        default=Path("config/endpoints.yaml"),
        help="Endpoints configuration file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without making API calls",
    )
    parser.add_argument(
        "--schemathesis-only",
        action="store_true",
        help="Only run Schemathesis tests",
    )
    parser.add_argument(
        "--endpoint",
        type=str,
        default=None,
        help="Filter to specific endpoint",
    )

    args = parser.parse_args()

    # Load configurations
    config = load_config(args.config)
    endpoints_config = load_endpoints_config(args.endpoints)

    # Initialize auth (skip in dry run)
    auth = None
    if not args.dry_run:
        try:
            auth = load_auth_from_config(config)
            if not auth.test_connection():
                console.print("[red]API connection failed[/red]")
                return 1
        except ValueError as e:
            console.print(f"[red]Auth error: {e}[/red]")
            console.print("[yellow]Run with --dry-run to skip API calls[/yellow]")
            return 1

    # Run validation
    orchestrator = ValidationOrchestrator(
        config=config,
        endpoints_config=endpoints_config,
        auth=auth,
        dry_run=args.dry_run,
    )

    return orchestrator.run(
        endpoint_filter=args.endpoint,
        schemathesis_only=args.schemathesis_only,
    )


if __name__ == "__main__":
    sys.exit(main())
