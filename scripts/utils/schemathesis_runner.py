"""Schemathesis integration for property-based API testing."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Generator
from enum import Enum

import schemathesis
from schemathesis import Case
from schemathesis.specs.openapi import definitions
from hypothesis import settings, Verbosity, Phase
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from .auth import F5XCAuth, RateLimiter
from .constraint_validator import Discrepancy, DiscrepancyType

console = Console()


class TestStatus(Enum):
    """Status of a Schemathesis test."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class SchemathesisResult:
    """Result from a Schemathesis test run."""

    endpoint: str
    method: str
    status: TestStatus
    examples_tested: int = 0
    failures: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    discrepancies: list[Discrepancy] = field(default_factory=list)


@dataclass
class SchemathesisConfig:
    """Configuration for Schemathesis testing."""

    max_examples: int = 100
    hypothesis_phases: list[str] = field(
        default_factory=lambda: ["generate", "target"]
    )
    stateful_testing: bool = True
    timeout_per_test: int = 60
    suppress_health_check: bool = True
    verbosity: str = "normal"


class SchemathesisRunner:
    """Run Schemathesis tests against F5 XC API."""

    def __init__(
        self,
        auth: F5XCAuth,
        config: Optional[SchemathesisConfig] = None,
    ):
        self.auth = auth
        self.config = config or SchemathesisConfig()
        self.results: list[SchemathesisResult] = []
        self._rate_limiter = RateLimiter()

        # Configure hypothesis settings
        self._hypothesis_settings = settings(
            max_examples=self.config.max_examples,
            phases=[
                Phase[phase.upper()]
                for phase in self.config.hypothesis_phases
                if hasattr(Phase, phase.upper())
            ],
            deadline=self.config.timeout_per_test * 1000,  # ms
            suppress_health_check=self.config.suppress_health_check,
            verbosity=Verbosity[self.config.verbosity.upper()],
        )

    def load_schema(self, spec: dict, base_url: Optional[str] = None) -> Any:
        """Load OpenAPI schema for Schemathesis."""
        base_url = base_url or self.auth.api_url

        # Create schema from dictionary
        schema = schemathesis.from_dict(
            spec,
            base_url=base_url,
            validate_schema=False,  # We may have intentionally invalid specs
        )

        return schema

    def load_schema_from_file(
        self,
        filepath: Path | str,
        base_url: Optional[str] = None,
    ) -> Any:
        """Load OpenAPI schema from file."""
        filepath = Path(filepath)
        base_url = base_url or self.auth.api_url

        schema = schemathesis.from_path(
            str(filepath),
            base_url=base_url,
            validate_schema=False,
        )

        return schema

    def run_tests(
        self,
        schema: Any,
        endpoint_filter: Optional[str] = None,
        method_filter: Optional[str] = None,
    ) -> list[SchemathesisResult]:
        """Run Schemathesis tests against the API."""
        results = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            # Filter endpoints if specified
            operations = list(schema.get_all_operations())
            if endpoint_filter:
                operations = [
                    op for op in operations
                    if endpoint_filter in op.path
                ]
            if method_filter:
                operations = [
                    op for op in operations
                    if op.method.upper() == method_filter.upper()
                ]

            task = progress.add_task(
                f"Testing {len(operations)} operations...",
                total=len(operations),
            )

            for operation in operations:
                result = self._test_operation(operation)
                results.append(result)
                progress.update(task, advance=1)

        self.results = results
        return results

    def _test_operation(self, operation: Any) -> SchemathesisResult:
        """Test a single API operation."""
        result = SchemathesisResult(
            endpoint=operation.path,
            method=operation.method.upper(),
            status=TestStatus.PASSED,
        )

        try:
            # Generate test cases
            test_cases = list(self._generate_test_cases(operation))
            result.examples_tested = len(test_cases)

            for case in test_cases:
                try:
                    # Rate limit
                    self._rate_limiter.wait_if_needed()

                    # Execute request
                    response = self._execute_case(case)

                    # Check for failures
                    if response.status_code >= 500:
                        result.errors.append({
                            "status_code": response.status_code,
                            "body": self._safe_json(response),
                            "case": self._case_to_dict(case),
                        })
                        result.status = TestStatus.ERROR

                    # Check for validation discrepancies
                    discrepancy = self._check_response(case, response)
                    if discrepancy:
                        result.discrepancies.append(discrepancy)
                        result.status = TestStatus.FAILED

                    self._rate_limiter.record_success()

                except Exception as e:
                    result.errors.append({
                        "error": str(e),
                        "case": self._case_to_dict(case),
                    })

        except Exception as e:
            result.status = TestStatus.ERROR
            result.errors.append({"error": str(e)})

        return result

    def _generate_test_cases(
        self,
        operation: Any,
        max_cases: int = 10,
    ) -> Generator[Case, None, None]:
        """Generate test cases for an operation using Hypothesis."""
        count = 0
        try:
            for case in operation.as_strategy().example():
                if count >= max_cases:
                    break
                yield case
                count += 1
        except Exception as e:
            console.print(
                f"[yellow]Failed to generate cases for {operation.path}: {e}[/yellow]"
            )

    def _execute_case(self, case: Case) -> Any:
        """Execute a test case against the API."""
        # Build request with authentication
        kwargs = case.as_transport_kwargs()

        # Add auth headers
        headers = kwargs.get("headers", {})
        headers.update(self.auth.headers)
        kwargs["headers"] = headers

        # Make request using auth client
        method = case.method.lower()
        path = case.formatted_path

        return self.auth.request(method.upper(), path, **kwargs)

    def _check_response(
        self,
        case: Case,
        response: Any,
    ) -> Optional[Discrepancy]:
        """Check response for validation discrepancies."""
        # Check if response matches expected schema
        status_code = str(response.status_code)

        # Get expected response schema
        operation = case.operation
        responses = operation.definition.get("responses", {})

        if status_code not in responses:
            # Check for default response
            if "default" not in responses:
                # Unexpected status code - potential discrepancy
                if response.status_code >= 400:
                    return Discrepancy(
                        path=case.path,
                        property_name="response",
                        constraint_type="status_code",
                        discrepancy_type=DiscrepancyType.CONSTRAINT_MISMATCH,
                        spec_value=list(responses.keys()),
                        api_behavior=status_code,
                        test_values=[self._case_to_dict(case)],
                        recommendation=f"Add {status_code} to response definitions",
                    )

        return None

    def _case_to_dict(self, case: Case) -> dict:
        """Convert a Schemathesis case to a dictionary for logging."""
        return {
            "path": case.path,
            "method": case.method,
            "path_parameters": case.path_parameters,
            "query": case.query,
            "body": case.body,
        }

    def _safe_json(self, response: Any) -> Any:
        """Safely extract JSON from response."""
        try:
            return response.json()
        except Exception:
            return response.text[:500] if hasattr(response, "text") else str(response)

    def run_stateful_tests(
        self,
        schema: Any,
        resource: str,
    ) -> list[SchemathesisResult]:
        """Run stateful CRUD workflow tests."""
        results = []

        # Define CRUD workflow
        crud_operations = ["create", "read", "update", "delete"]

        console.print(f"[blue]Running stateful tests for {resource}[/blue]")

        for operation in crud_operations:
            # Find matching endpoint
            for op in schema.get_all_operations():
                if resource in op.path:
                    method = op.method.upper()
                    if self._matches_crud_operation(method, op.path, operation):
                        result = self._test_operation(op)
                        results.append(result)
                        break

        return results

    def _matches_crud_operation(
        self,
        method: str,
        path: str,
        operation: str,
    ) -> bool:
        """Check if method/path matches expected CRUD operation."""
        crud_map = {
            "create": ("POST", False),  # POST without {name}
            "read": ("GET", True),  # GET with {name}
            "list": ("GET", False),  # GET without {name}
            "update": ("PUT", True),  # PUT with {name}
            "delete": ("DELETE", True),  # DELETE with {name}
        }

        if operation not in crud_map:
            return False

        expected_method, needs_name = crud_map[operation]
        has_name = "{name}" in path

        return method == expected_method and has_name == needs_name

    def get_summary(self) -> dict:
        """Get summary of test results."""
        total = len(self.results)
        passed = sum(1 for r in self.results if r.status == TestStatus.PASSED)
        failed = sum(1 for r in self.results if r.status == TestStatus.FAILED)
        errors = sum(1 for r in self.results if r.status == TestStatus.ERROR)

        total_examples = sum(r.examples_tested for r in self.results)
        total_discrepancies = sum(len(r.discrepancies) for r in self.results)

        return {
            "total_operations": total,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "total_examples_tested": total_examples,
            "total_discrepancies": total_discrepancies,
            "pass_rate": passed / total if total > 0 else 0,
        }


def create_runner(
    auth: F5XCAuth,
    config: Optional[dict] = None,
) -> SchemathesisRunner:
    """Create a Schemathesis runner with configuration."""
    schemathesis_config = None
    if config:
        schemathesis_config = SchemathesisConfig(
            max_examples=config.get("max_examples", 100),
            hypothesis_phases=config.get("hypothesis_phases", ["generate", "target"]),
            stateful_testing=config.get("stateful_testing", True),
        )

    return SchemathesisRunner(auth, schemathesis_config)
