"""Schemathesis integration for property-based API testing."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Generator

import schemathesis
from hypothesis import Phase, given, settings
from requests.structures import CaseInsensitiveDict
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from schemathesis import Case
from schemathesis.checks import CHECKS, CheckFunction, FailureGroup, load_all_checks
from schemathesis.config import ProjectConfig, ProjectsConfig
from schemathesis.config import SchemathesisConfig as LibrarySchemathesisConfig

from .auth import F5XCAuth, RateLimiter
from .constraint_validator import Discrepancy, DiscrepancyType

console = Console()

# HTTP status code thresholds
HTTP_SERVER_ERROR = 500
HTTP_CLIENT_ERROR = 400

# Schemathesis' default validation set includes active checks such as
# ``ignored_auth`` that issue another HTTP request.  Live validation has already
# executed the configured case exactly once; its result must depend only on that
# response.  Resolve a closed, pinned set of passive response checks up front so
# repository or library configuration cannot silently add network activity.
RESPONSE_ONLY_CHECK_NAMES = (
    "not_a_server_error",
    "status_code_conformance",
    "content_type_conformance",
    "response_headers_conformance",
    "response_schema_conformance",
)
load_all_checks()
RESPONSE_ONLY_CHECKS = cast(
    list[CheckFunction],
    CHECKS.get_by_names(RESPONSE_ONLY_CHECK_NAMES),
)


def _invoke_hypothesis_wrapper(test: Any) -> None:
    """Call a ``@given`` wrapper whose generated arguments are runtime-owned."""
    test()


class OperationResolutionError(RuntimeError):
    """Raised when the OpenAPI operation graph cannot satisfy the test contract."""


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

    examples_per_operation: int = 1

    def __post_init__(self) -> None:
        """Reject configurations that could produce vacuous operation results."""
        if (
            not isinstance(self.examples_per_operation, int)
            or isinstance(self.examples_per_operation, bool)
            or self.examples_per_operation <= 0
        ):
            raise ValueError("schemathesis.examples_per_operation must be a positive integer")


class SchemathesisRunner:
    """Run Schemathesis tests against F5 XC API."""

    def __init__(
        self,
        auth: F5XCAuth,
        config: SchemathesisConfig | None = None,
    ) -> None:
        """Initialize SchemathesisRunner with auth and config."""
        self.auth = auth
        self.config = config or SchemathesisConfig()
        self.results: list[SchemathesisResult] = []
        self._rate_limiter = RateLimiter()

    def load_schema(self, spec: dict, base_url: str | None = None) -> Any:
        """Load OpenAPI schema for Schemathesis."""
        base_url = base_url or self.auth.api_url

        # In Schemathesis 4.x, the concrete base URL must be in the schema used
        # to build cases. Published specs intentionally carry a tenant template;
        # the authenticated validation copy replaces it without mutating input.
        spec_copy = spec.copy()
        if base_url:
            spec_copy["servers"] = [{"url": base_url}]

        library_config = LibrarySchemathesisConfig(
            projects=ProjectsConfig(default=ProjectConfig(base_url=base_url))
        )
        return schemathesis.openapi.from_dict(spec_copy, config=library_config)

    def load_schema_from_file(
        self,
        filepath: Path | str,
        base_url: str | None = None,
    ) -> Any:
        """Load OpenAPI schema from file."""
        filepath = Path(filepath)
        base_url = base_url or self.auth.api_url

        # Load spec from file first
        with filepath.open() as f:
            spec = json.load(f)

        # Replace the published tenant template in this validation-only copy.
        if base_url:
            spec["servers"] = [{"url": base_url}]

        library_config = LibrarySchemathesisConfig(
            projects=ProjectsConfig(default=ProjectConfig(base_url=base_url))
        )
        return schemathesis.openapi.from_dict(spec, config=library_config)

    def run_tests(
        self,
        schema: Any,
        endpoint_filter: str | None = None,
        method_filter: str | None = None,
    ) -> list[SchemathesisResult]:
        """Run Schemathesis tests against the API."""
        results = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            all_operations = list(self._collect_operations(schema).values())

            # Filter endpoints if specified
            operations = all_operations
            if endpoint_filter:
                operations = [op for op in operations if endpoint_filter in op.path]
            if method_filter:
                operations = [op for op in operations if op.method.upper() == method_filter.upper()]

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
            test_cases = list(
                self._generate_test_cases(
                    operation,
                    max_cases=self.config.examples_per_operation,
                )
            )
            result.examples_tested = len(test_cases)

            for case in test_cases:
                try:
                    # Rate limit
                    self._rate_limiter.wait_if_needed()

                    # Execute request
                    response = self._execute_case(case)

                    # Check for failures
                    if response.status_code >= HTTP_SERVER_ERROR:
                        result.errors.append(
                            {
                                "status_code": response.status_code,
                                "case": self._case_to_dict(case),
                            }
                        )
                        result.status = TestStatus.ERROR

                    # Validate response against schema using Schemathesis
                    try:
                        case.validate_response(
                            response,
                            checks=RESPONSE_ONLY_CHECKS,
                        )
                    except FailureGroup as failure_group:
                        # A Schemathesis failure is measured contract evidence.
                        # Keep it distinct from unexpected validator failures,
                        # which mean the validation mechanism itself errored.
                        validation_errors: tuple[BaseException, ...] = tuple(
                            failure_group.exceptions
                        )
                        for validation_error in validation_errors:
                            result.discrepancies.append(
                                self._make_schema_discrepancy(case, validation_error)
                            )
                        if result.status is not TestStatus.ERROR:
                            result.status = TestStatus.FAILED
                    except Exception as validation_error:  # pylint: disable=broad-exception-caught
                        result.errors.append(
                            {
                                "error": self._redact_namespace(str(validation_error)),
                                "stage": "response_validation",
                                "case": self._case_to_dict(case),
                            }
                        )
                        result.status = TestStatus.ERROR

                    # Additional validation checks
                    response_discrepancy = self._check_response(case, response)
                    if response_discrepancy:
                        result.discrepancies.append(response_discrepancy)
                        if result.status is not TestStatus.ERROR:
                            result.status = TestStatus.FAILED

                    self._rate_limiter.record_success()

                except Exception as e:  # pylint: disable=broad-exception-caught
                    result.errors.append(
                        {
                            "error": str(e),
                            "case": self._case_to_dict(case),
                        }
                    )
                    result.status = TestStatus.ERROR

        except Exception as e:  # pylint: disable=broad-exception-caught
            result.status = TestStatus.ERROR
            result.errors.append({"error": str(e)})

        return result

    def _generate_test_cases(
        self,
        operation: Any,
        max_cases: int = 10,
    ) -> Generator[Case]:
        """Generate test cases for an operation using Hypothesis.

        Generation errors deliberately propagate to :meth:`_test_operation`,
        which records an ERROR result.  Returning an empty generator here used
        to turn an untestable operation into a false pass with zero examples.
        """
        generated: list[Case] = []

        @settings(
            max_examples=max_cases,
            derandomize=True,
            database=None,
            deadline=None,
            phases=(Phase.generate,),
        )
        @given(case=operation.as_strategy())
        def collect(case: Case) -> None:
            generated.append(case)

        _invoke_hypothesis_wrapper(collect)
        yield from generated

    def _execute_case(self, case: Case) -> Any:
        """Execute a test case against the API."""
        self._bind_validation_scope(case)

        # Build request with authentication
        kwargs = case.as_transport_kwargs()

        # Remove method and url from kwargs to avoid "got multiple values for argument" error
        # since we pass them as positional arguments to auth.request()
        kwargs.pop("method", None)
        kwargs.pop("url", None)

        # Add auth headers
        headers = kwargs.get("headers", {})
        headers.update(self.auth.headers)
        kwargs["headers"] = headers

        # Make request using auth client
        method = case.method.upper()
        path = case.formatted_path

        return self.auth.request(method, path, **kwargs)

    def _bind_validation_scope(self, case: Case) -> None:
        """Force a safe, authenticated, deterministic read request."""
        parameters = case.path_parameters
        if parameters:
            for name in parameters:
                if name == "namespace" or name.endswith(".namespace"):
                    parameters[name] = self.auth.namespace

        headers = CaseInsensitiveDict(case.headers or {})
        headers.update(self.auth.headers)
        case.headers = headers

        # The production gate intentionally owns only read/list response
        # contracts. Optional fuzzed query combinations produced server errors
        # and made the same committed contract depend on generated input.
        if case.method.upper() == "GET":
            case.query = {}

    def _redact_namespace(self, value: str) -> str:
        """Remove the infrastructure namespace from persisted evidence."""
        return value.replace(self.auth.namespace, "<configured-namespace>")

    def _make_schema_discrepancy(self, case: Case, validation_error: BaseException) -> Discrepancy:
        """Create a schema validation discrepancy from a validation error."""
        redacted_error = self._redact_namespace(str(validation_error))
        return Discrepancy(
            path=case.path,
            property_name="response_schema",
            constraint_type="schema_validation",
            discrepancy_type=DiscrepancyType.CONSTRAINT_MISMATCH,
            spec_value="Valid per OpenAPI schema",
            api_behavior=redacted_error,
            test_values=[self._case_to_dict(case)],
            recommendation=f"Update schema or fix API response: {redacted_error}",
        )

    def _check_response(
        self,
        case: Case,
        response: Any,
    ) -> Discrepancy | None:
        """Check response for validation discrepancies."""
        # Check if response matches expected schema
        status_code = str(response.status_code)

        # Get expected response schema
        try:
            operation = case.operation
            # In Schemathesis 4.x, definition might be an object or dict
            definition = operation.definition
            if isinstance(definition, dict):
                responses = definition.get("responses", {})
            elif hasattr(definition, "responses"):
                responses = definition.responses
            else:
                # Can't validate - skip check
                return None

            if (
                status_code not in responses
                and "default" not in responses
                and response.status_code >= HTTP_CLIENT_ERROR
            ):
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
        except Exception:  # pylint: disable=broad-exception-caught
            # If we can't get the response schema, skip validation
            return None

        return None

    def _case_to_dict(self, case: Case) -> dict:
        """Convert a Schemathesis case to a dictionary for logging."""
        path_parameters = {
            name: (
                "<configured-namespace>"
                if name == "namespace" or name.endswith(".namespace")
                else value
            )
            for name, value in (case.path_parameters or {}).items()
        }
        return {
            "path": case.path,
            "method": case.method,
            "path_parameters": path_parameters,
            "query": case.query,
            "body": None if type(case.body).__name__ == "NotSet" else case.body,
        }

    def _collect_operations(
        self,
        schema: Any,
        required: set[tuple[str, str]] | None = None,
    ) -> dict[tuple[str, str], Any]:
        """Return every valid schema operation indexed by exact method/path.

        Schemathesis exposes parse failures as ``Err`` result objects. Exact
        configured operations fail on their corresponding error; unrelated
        mutation operations are outside the read-only gate and are not loaded.
        """
        operations: dict[tuple[str, str], Any] = {}
        for operation_result in schema.get_all_operations():
            error_getter = getattr(operation_result, "err", None)
            if callable(error_getter):
                error = error_getter()
                error_path = getattr(error, "path", None)
                error_method = getattr(error, "method", None)
                identity = (
                    (str(error_method).upper(), str(error_path))
                    if error_method and error_path
                    else None
                )
                if required is None or identity is None or identity in required:
                    cause = f": {error.__cause__}" if error.__cause__ else ""
                    raise OperationResolutionError(
                        f"invalid OpenAPI operation {identity or '<unknown>'}: {error}{cause}"
                    )
                continue

            try:
                operation = (
                    operation_result.ok()
                    if hasattr(operation_result, "ok") and callable(operation_result.ok)
                    else operation_result
                )
            except Exception as error:
                raise OperationResolutionError(f"invalid OpenAPI operation: {error}") from error

            if not hasattr(operation, "path") or not hasattr(operation, "method"):
                raise OperationResolutionError("OpenAPI operation has no method/path identity")

            key = (str(operation.method).upper(), str(operation.path))
            if key in operations:
                raise OperationResolutionError(
                    f"OpenAPI operation is duplicated: {key[0]} {key[1]}"
                )
            operations[key] = operation

        if not operations:
            raise OperationResolutionError("OpenAPI schema resolved to zero operations")
        return operations

    def run_configured_operations(
        self,
        schema: Any,
        configured_operations: tuple[tuple[str, str], ...],
    ) -> list[SchemathesisResult]:
        """Execute only the exact operations declared by validation config."""
        if not configured_operations:
            raise OperationResolutionError("validation target declared zero operations")

        required = {(method.upper(), path) for method, path in configured_operations}
        available = self._collect_operations(schema, required=required)
        selected = []
        for method, path in configured_operations:
            key = (method.upper(), path)
            operation = available.get(key)
            if operation is None:
                raise OperationResolutionError(
                    f"configured OpenAPI operation was not resolved: {key[0]} {key[1]}"
                )
            selected.append(operation)

        results = [self._test_operation(operation) for operation in selected]
        self.results.extend(results)
        return results

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
    config: dict | None = None,
) -> SchemathesisRunner:
    """Create a Schemathesis runner with configuration."""
    schemathesis_config = None
    if config:
        schemathesis_config = SchemathesisConfig(
            examples_per_operation=config.get("examples_per_operation", 1),
        )

    return SchemathesisRunner(auth, schemathesis_config)
