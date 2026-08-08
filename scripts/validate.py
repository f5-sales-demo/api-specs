"""Validation orchestrator for F5 XC API specs."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml
from rich.console import Console

from .utils.auth import F5XCAuth, load_auth_from_config
from .utils.constraint_validator import Discrepancy
from .utils.report_generator import create_report_generator
from .utils.schemathesis_runner import (
    OperationResolutionError,
    SchemathesisResult,
    SchemathesisRunner,
    TestStatus,
    create_runner,
)
from .utils.spec_loader import SpecLoader

console = Console()


# Filename prefix/suffix patterns for F5 XC spec files:
# ``docs-cloud-f5-com.NNNN.public.ves.io.schema.<domain>.ves-swagger.json``
# The domain slug is the segment between ``schema.`` and ``.ves-swagger``
# (or the final stem if the pattern doesn't match).
_DOMAIN_FILENAME_PREFIX = "public.ves.io.schema."
_DOMAIN_FILENAME_SUFFIX = ".ves-swagger"
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


class LiveValidationError(RuntimeError):
    """Raised when a live validation run cannot prove its test contract."""


@dataclass(frozen=True)
class ValidationTarget:
    """One semantically resolved spec and its exact live operations."""

    endpoint_name: str
    domain: str
    filename: str
    operations: tuple[tuple[str, str], ...]


def _domain_from_filename(filename: str) -> str:
    """Derive the F5 XC domain slug from a spec filename.

    Example:
        >>> _domain_from_filename(
        ...     "docs-cloud-f5-com.0041.public.ves.io.schema.origin_pool."
        ...     "ves-swagger.json"
        ... )
        'origin_pool'

    Returns ``"unknown"`` if the filename doesn't match the expected
    pattern.
    """
    if not filename:
        return "unknown"
    stem = Path(filename).stem  # drop .json / .yaml
    # Strip nested ".ves-swagger" suffix if present.
    stem = stem.removesuffix(_DOMAIN_FILENAME_SUFFIX)
    idx = stem.find(_DOMAIN_FILENAME_PREFIX)
    if idx != -1:
        return stem[idx + len(_DOMAIN_FILENAME_PREFIX) :] or "unknown"
    return stem or "unknown"


def load_config(config_path: Path) -> dict:
    """Load configuration from YAML file."""
    if not config_path.exists():
        console.print(f"[red]Config file not found: {config_path}[/red]")
        sys.exit(1)

    with config_path.open() as f:
        result: dict = yaml.safe_load(f)
        return result


def load_endpoints_config(config_path: Path) -> dict:
    """Load endpoints configuration."""
    if not config_path.exists():
        console.print(f"[red]Endpoints config not found: {config_path}[/red]")
        sys.exit(1)

    with config_path.open() as f:
        result: dict = yaml.safe_load(f)
        return result


def _parse_operation(endpoint_name: str, operation_name: str, value: object) -> tuple[str, str]:
    """Parse a configured ``METHOD /path`` declaration."""
    if not isinstance(value, str):
        raise LiveValidationError(
            f"endpoint '{endpoint_name}' operation '{operation_name}' must be METHOD /path"
        )
    method, separator, path = value.partition(" ")
    method = method.upper()
    if not separator or method not in _HTTP_METHODS or not path.startswith("/") or " " in path:
        raise LiveValidationError(
            f"endpoint '{endpoint_name}' operation '{operation_name}' must be METHOD /path"
        )
    return method, path


def resolve_validation_targets(
    specs: dict[str, dict],
    endpoints_config: dict,
) -> tuple[ValidationTarget, ...]:
    """Bind semantic domain identifiers to exact downloaded spec operations.

    Numeric filename prefixes are publication ordering metadata and change on
    each upstream drop.  Configuration therefore owns the stable domain slug
    between ``schema.`` and ``.ves-swagger`` and must resolve it uniquely.
    """
    endpoint_map = endpoints_config.get("endpoints")
    if not isinstance(endpoint_map, dict) or not endpoint_map:
        raise LiveValidationError("endpoints configuration must declare at least one endpoint")

    test_order = endpoints_config.get("test_order")
    if (
        not isinstance(test_order, list)
        or any(not isinstance(name, str) for name in test_order)
        or len(test_order) != len(set(test_order))
        or set(test_order) != set(endpoint_map)
    ):
        raise LiveValidationError("test_order must name every configured endpoint exactly once")

    files_by_domain: dict[str, list[str]] = {}
    for filename in specs:
        files_by_domain.setdefault(_domain_from_filename(filename), []).append(filename)

    targets: list[ValidationTarget] = []
    for endpoint_name in test_order:
        endpoint = endpoint_map[endpoint_name]
        if not isinstance(endpoint, dict):
            raise LiveValidationError(f"endpoint '{endpoint_name}' configuration must be a mapping")

        domain = endpoint.get("domain")
        if not isinstance(domain, str) or not domain:
            raise LiveValidationError(f"endpoint '{endpoint_name}' must declare semantic domain")

        matching_files = sorted(files_by_domain.get(domain, []))
        if len(matching_files) != 1:
            raise LiveValidationError(
                f"endpoint '{endpoint_name}' domain '{domain}' resolved to "
                f"{len(matching_files)} files"
            )
        filename = matching_files[0]
        spec = specs[filename]

        configured_operations = endpoint.get("operations")
        if not isinstance(configured_operations, dict) or not configured_operations:
            raise LiveValidationError(
                f"endpoint '{endpoint_name}' must declare at least one operation"
            )

        operations: list[tuple[str, str]] = []
        for operation_name, value in configured_operations.items():
            method, path = _parse_operation(endpoint_name, str(operation_name), value)
            path_item = spec.get("paths", {}).get(path)
            if not isinstance(path_item, dict) or method.lower() not in path_item:
                raise LiveValidationError(
                    f"endpoint '{endpoint_name}' configured operation does not exist in "
                    f"domain '{domain}': {method} {path}"
                )
            operations.append((method, path))

        if len(operations) != len(set(operations)):
            raise LiveValidationError(
                f"endpoint '{endpoint_name}' declares a duplicate method/path operation"
            )
        targets.append(
            ValidationTarget(
                endpoint_name=endpoint_name,
                domain=domain,
                filename=filename,
                operations=tuple(operations),
            )
        )

    return tuple(targets)


def validate_live_results(
    target: ValidationTarget,
    expected_operations: tuple[tuple[str, str], ...],
    results: list[SchemathesisResult],
) -> None:
    """Require execution evidence for every configured operation."""
    if len(results) != len(expected_operations):
        raise LiveValidationError(
            f"endpoint '{target.endpoint_name}' executed {len(results)} of "
            f"{len(expected_operations)} configured operations"
        )

    expected = set(expected_operations)
    actual = {(result.method.upper(), result.endpoint) for result in results}
    if len(actual) != len(results) or actual != expected:
        raise LiveValidationError(
            f"endpoint '{target.endpoint_name}' execution identities do not match configuration"
        )

    for result in results:
        identity = f"{result.method.upper()} {result.endpoint}"
        if result.examples_tested <= 0:
            raise LiveValidationError(f"configured operation {identity} executed zero examples")
        if result.status in {TestStatus.ERROR, TestStatus.SKIPPED} or result.errors:
            raise LiveValidationError(f"configured operation {identity} finished with error status")


class ValidationOrchestrator:  # pylint: disable=too-many-instance-attributes
    """Orchestrate validation of F5 XC API specs.

    The attribute count is intentional — this class aggregates the
    reconcile-side state machine (config, endpoints, auth, results,
    discrepancies, and their domain/method sidecars introduced in
    Task A7) and splitting it would just shuffle the same data around.
    """

    def __init__(
        self,
        config: dict,
        endpoints_config: dict,
        auth: F5XCAuth,
    ) -> None:
        """Initialize ValidationOrchestrator with config and auth."""
        self.config = config
        self.endpoints_config = endpoints_config

        # Initialize components
        self.spec_loader = SpecLoader(
            Path(config.get("validation", {}).get("input_dir", "specs/transformed"))
        )
        self.auth = auth
        self.schemathesis_runner: SchemathesisRunner = create_runner(
            auth,
            config.get("schemathesis", {}),
        )

        # Report generator
        self.report_generator = create_report_generator(config.get("reports", {}))

        # Results storage
        self.discrepancies: list[Discrepancy] = []
        self.test_results: list[SchemathesisResult] = []

    def _prepare_validation_targets(
        self,
        endpoint_filter: str | None = None,
    ) -> tuple[dict[str, dict], tuple[ValidationTarget, ...]] | None:
        """Load, validate specs, resolve validation targets, and filter them."""
        # Step 1: Load and validate specs
        console.print("\n[bold]Step 1: Loading OpenAPI Specs[/bold]")
        specs = self._load_specs()

        if not specs:
            console.print("[red]No specs found. Run 'make download' first.[/red]")
            return None

        # Step 2: Validate spec structure
        console.print("\n[bold]Step 2: Validating Spec Structure[/bold]")
        structure_errors = self._validate_spec_structure(specs)
        if structure_errors:
            console.print(f"[red]Validation stopped: {len(structure_errors)} invalid specs[/red]")
            return None

        # Step 3: Resolve every semantic domain and exact operation before
        # making requests. A stale config must fail rather than test nothing.
        console.print("\n[bold]Step 3: Resolving Validation Contract[/bold]")
        try:
            targets = resolve_validation_targets(specs, self.endpoints_config)
        except LiveValidationError as error:
            console.print(f"[red]Validation contract error: {error}[/red]")
            return None

        if endpoint_filter:
            targets = tuple(target for target in targets if target.endpoint_name == endpoint_filter)
            if not targets:
                console.print(
                    f"[red]Validation contract error: unknown endpoint '{endpoint_filter}'[/red]"
                )
                return None

        return specs, targets

    def run(
        self,
        endpoint_filter: str | None = None,
        allow_discrepancies: bool = False,
    ) -> int:
        """Run the full validation pipeline."""
        console.print("[bold blue]F5 XC API Spec Validation[/bold blue]")

        prep_result = self._prepare_validation_targets(endpoint_filter)
        if prep_result is None:
            return 1

        specs, targets = prep_result

        # Step 4: Execute exact configured operations and retain all evidence,
        # even when one target fails, so the report explains the failed gate.
        console.print("\n[bold]Step 4: Running Authenticated Schemathesis Tests[/bold]")
        execution_errors = self._run_schemathesis_tests(specs, targets)

        # Step 5: Generate reports
        console.print("\n[bold]Step 5: Generating Reports[/bold]")
        self._generate_reports()

        # Print summary
        self._print_summary()

        if execution_errors:
            console.print("\n[bold red]Live validation contract failures:[/bold red]")
            for execution_error in execution_errors:
                console.print(f"  [red]- {execution_error}[/red]")
            return 1

        if self.discrepancies and not allow_discrepancies:
            return 1

        return 0

    def _load_specs(self) -> dict[str, dict]:
        """Load all OpenAPI specs."""
        try:
            specs = self.spec_loader.load_all_domain_files()
            console.print(f"[green]Loaded {len(specs)} domain specs[/green]")
            return specs
        except Exception as e:  # pylint: disable=broad-exception-caught
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

        if not errors:
            console.print(f"[green]Validated {len(specs)} OpenAPI specs[/green]")

        return errors

    def _run_schemathesis_tests(
        self,
        specs: dict[str, dict],
        targets: tuple[ValidationTarget, ...],
    ) -> list[str]:
        """Run exact configured operations and return contract failures."""
        errors: list[str] = []
        for target in targets:
            console.print(f"\n[cyan]Testing {target.endpoint_name} ({target.domain})[/cyan]")
            try:
                schema = self.schemathesis_runner.load_schema(specs[target.filename])
                results = self.schemathesis_runner.run_configured_operations(
                    schema,
                    target.operations,
                )
                self.test_results.extend(results)

                for result in results:
                    self.discrepancies.extend(result.discrepancies)
                    for d in result.discrepancies:
                        d.spec_file = target.filename
                        d.domain = target.domain
                        d.method = result.method
                validate_live_results(target, target.operations, results)
            except (LiveValidationError, OperationResolutionError) as error:
                errors.append(str(error))
            except Exception as error:  # pylint: disable=broad-exception-caught
                errors.append(f"endpoint '{target.endpoint_name}' could not execute: {error}")
        return errors

    def _generate_reports(self) -> None:
        """Generate validation reports."""
        # Determine modified vs unmodified files
        modified_files = []
        unmodified_files = []

        # For now, all files are considered unmodified until reconciliation
        specs = self.spec_loader.load_all_domain_files()
        for filename in specs:
            if any(d.spec_file == filename for d in self.discrepancies):
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
            by_type: dict[str, int] = {}
            for d in self.discrepancies:
                dtype = d.discrepancy_type.value
                by_type[dtype] = by_type.get(dtype, 0) + 1

            console.print("\n[bold]Discrepancies by type:[/bold]")
            for dtype, count in sorted(by_type.items()):
                console.print(f"  {dtype}: {count}")

        if self.schemathesis_runner:
            summary = self.schemathesis_runner.get_summary()
            console.print("\n[bold]Schemathesis Summary:[/bold]")
            console.print(f"  Operations tested: {summary['total_operations']}")
            console.print(f"  Pass rate: {summary['pass_rate']:.1%}")


def main() -> int:
    """Main entry point for validation command."""
    parser = argparse.ArgumentParser(description="Validate F5 XC OpenAPI specs against live API")
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
        "--endpoint",
        type=str,
        default=None,
        help="Filter to specific endpoint",
    )
    parser.add_argument(
        "--allow-discrepancies",
        action="store_true",
        help="Always return exit 0 if validation execution runs cleanly, even with discrepancies.",
    )

    args = parser.parse_args()

    # Load configurations
    config = load_config(args.config)
    endpoints_config = load_endpoints_config(args.endpoints)

    # Authenticated execution is the only validation mode. Static checks have
    # separate commands and must never produce a live-validation success report.
    try:
        auth = load_auth_from_config(config)
        if not auth.test_connection():
            console.print("[red]API authentication/connection check failed[/red]")
            return 1
    except ValueError as error:
        console.print(f"[red]Auth error: {error}[/red]")
        return 1

    # Run validation
    orchestrator = ValidationOrchestrator(
        config=config,
        endpoints_config=endpoints_config,
        auth=auth,
    )

    return orchestrator.run(
        endpoint_filter=args.endpoint,
        allow_discrepancies=args.allow_discrepancies,
    )


if __name__ == "__main__":
    sys.exit(main())
