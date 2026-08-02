"""Operation-resolution contracts for the Schemathesis runner."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Protocol, cast

import pytest
import requests
import schemathesis
from hypothesis import strategies as st
from schemathesis.checks import FailureGroup, ServerError
from schemathesis.core.transport import Response as SchemathesisResponse

from scripts.utils.schemathesis_runner import (
    RESPONSE_ONLY_CHECK_NAMES,
    RESPONSE_ONLY_CHECKS,
    OperationResolutionError,
    SchemathesisConfig,
    SchemathesisResult,
    SchemathesisRunner,
)
from scripts.utils.schemathesis_runner import (
    TestStatus as _TestStatus,
)


class _Ok:
    def __init__(self, value: object) -> None:
        self.value = value

    def ok(self) -> object:
        return self.value


class _Err:
    def ok(self) -> object:
        raise ValueError("invalid OpenAPI operation")


class _Schema:
    def __init__(self, *operations: object) -> None:
        self.operations = operations

    def get_all_operations(self):
        return iter(self.operations)


class _Operation(Protocol):
    path: str
    method: str


class _TestableRunner(SchemathesisRunner):
    def generated_cases(self, operation: object, max_cases: int) -> list[object]:
        return list(self._generate_test_cases(operation, max_cases=max_cases))

    def bind_scope(self, case: object) -> None:
        self._bind_validation_scope(cast(Any, case))

    def case_evidence(self, case: object) -> dict:
        return self._case_to_dict(cast(Any, case))

    def test_operation(self, operation: object) -> SchemathesisResult:
        return self._test_operation(operation)

    def set_rate_limiter(self, limiter: object) -> None:
        self._rate_limiter = cast(Any, limiter)


def _runner(monkeypatch: pytest.MonkeyPatch) -> SchemathesisRunner:
    runner = object.__new__(SchemathesisRunner)
    runner.results = []

    def execute(operation: _Operation) -> SchemathesisResult:
        return SchemathesisResult(
            operation.path,
            operation.method.upper(),
            _TestStatus.PASSED,
            examples_tested=1,
        )

    monkeypatch.setattr(runner, "_test_operation", execute)
    return runner


def test_exact_configured_operations_run_in_declared_order(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner(monkeypatch)
    list_op = SimpleNamespace(method="get", path="/items")
    create_op = SimpleNamespace(method="post", path="/items")
    schema = _Schema(_Ok(create_op), _Ok(list_op))

    results = runner.run_configured_operations(
        schema,
        (("GET", "/items"), ("POST", "/items")),
    )

    assert [(result.method, result.endpoint) for result in results] == [
        ("GET", "/items"),
        ("POST", "/items"),
    ]


def test_missing_configured_operation_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner(monkeypatch)
    schema = _Schema(_Ok(SimpleNamespace(method="get", path="/items")))

    with pytest.raises(OperationResolutionError, match="POST /items"):
        runner.run_configured_operations(schema, (("POST", "/items"),))


def test_invalid_openapi_operation_is_not_silently_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner(monkeypatch)

    with pytest.raises(OperationResolutionError, match="invalid OpenAPI operation"):
        runner.run_configured_operations(_Schema(_Err()), (("GET", "/items"),))


def test_generated_examples_are_derandomized() -> None:
    runner = object.__new__(_TestableRunner)
    operation = SimpleNamespace(as_strategy=st.integers)

    first = runner.generated_cases(operation, max_cases=10)
    second = runner.generated_cases(operation, max_cases=10)

    assert first == second
    assert first


@pytest.mark.parametrize("value", [0, -1, True])
def test_examples_per_operation_must_be_positive(value: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        SchemathesisConfig(examples_per_operation=value)


def test_load_schema_replaces_published_server_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = object.__new__(_TestableRunner)
    runner.auth = cast(Any, SimpleNamespace(api_url="https://live.example.invalid"))
    captured: dict[str, Any] = {}

    def capture(spec: dict, *, config: object) -> str:
        captured.update(spec)
        captured["config"] = config
        return "schema"

    monkeypatch.setattr(
        "scripts.utils.schemathesis_runner.schemathesis.openapi.from_dict",
        capture,
    )
    published = {"servers": [{"url": "https://{tenant}.example.invalid"}]}

    assert runner.load_schema(published) == "schema"
    assert captured["servers"] == [{"url": "https://live.example.invalid"}]
    assert captured["config"].projects.default.base_url == "https://live.example.invalid"
    assert published["servers"] == [{"url": "https://{tenant}.example.invalid"}]


def test_namespace_scope_is_injected_and_redacted() -> None:
    runner = object.__new__(_TestableRunner)
    runner.auth = cast(
        Any,
        SimpleNamespace(
            namespace="example-namespace",
            headers={"Authorization": "APIToken secret"},
        ),
    )
    case = SimpleNamespace(
        path="/namespaces/{namespace}/items",
        method="GET",
        path_parameters={"namespace": "example-namespace", "name": "generated-name"},
        headers={"Authorization": "generated-invalid-auth"},
        query={"fuzzed": "value"},
        body=None,
    )

    runner.bind_scope(case)
    evidence = runner.case_evidence(case)

    assert case.path_parameters["namespace"] == "private-namespace"
    assert case.headers == {"Authorization": "APIToken secret"}
    assert case.query == {}
    assert evidence["path_parameters"] == {
        "namespace": "example-namespace",
        "name": "generated-name",
    }
    assert "private-namespace" not in str(evidence)


class _NoWaitRateLimiter:
    def wait_if_needed(self) -> None:
        pass

    def record_success(self) -> None:
        pass


class _Response:
    status_code = 200


def _single_case_runner(monkeypatch: pytest.MonkeyPatch, case: object) -> _TestableRunner:
    runner = object.__new__(_TestableRunner)
    runner.auth = cast(Any, SimpleNamespace(namespace="example-namespace"))
    runner.config = SchemathesisConfig(examples_per_operation=1)
    runner.results = []
    runner.set_rate_limiter(_NoWaitRateLimiter())
    monkeypatch.setattr(runner, "_generate_test_cases", lambda *_args, **_kwargs: iter((case,)))
    monkeypatch.setattr(runner, "_execute_case", lambda _case: _Response())
    monkeypatch.setattr(runner, "_check_response", lambda *_args: None)
    return runner


def test_response_validation_uses_only_passive_checks_and_never_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Case:
        path = "/items"
        method = "GET"
        path_parameters: dict[str, object] = {}
        query: dict[str, object] = {}
        body = None

        def validate_response(
            self, response: object, *, checks: list[object] | None = None
        ) -> None:
            captured["response"] = response
            captured["checks"] = checks

    result = _single_case_runner(monkeypatch, Case()).test_operation(
        SimpleNamespace(path="/items", method="get")
    )

    assert result.status is _TestStatus.PASSED
    assert isinstance(captured["response"], _Response)
    assert captured["response"].status_code == 200
    checks = captured["checks"]
    assert isinstance(checks, list)
    assert tuple(check.__name__ for check in checks) == RESPONSE_ONLY_CHECK_NAMES
    assert "ignored_auth" not in RESPONSE_ONLY_CHECK_NAMES


def test_response_only_checks_do_not_call_the_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = schemathesis.openapi.from_dict(
        {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1"},
            "security": [{"token": []}],
            "components": {
                "securitySchemes": {
                    "token": {
                        "type": "apiKey",
                        "in": "header",
                        "name": "Authorization",
                    }
                }
            },
            "paths": {
                "/items": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "OK",
                                "content": {"application/json": {"schema": {"type": "object"}}},
                            }
                        }
                    }
                }
            },
        }
    )
    operation = cast(Any, next(schema.get_all_operations())).ok()
    case = operation.Case(headers={"Authorization": "APIToken placeholder"})

    def unexpected_secondary_request(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a response-only check issued a secondary request")

    monkeypatch.setattr(schema.transport, "send", unexpected_secondary_request)
    request = requests.Request(
        "GET",
        "https://example.invalid/items",
        headers={"Authorization": "APIToken placeholder"},
    ).prepare()
    response = SchemathesisResponse(
        status_code=200,
        headers={"content-type": ["application/json"]},
        content=b"{}",
        request=request,
        elapsed=0,
        verify=True,
    )

    case.validate_response(response, checks=RESPONSE_ONLY_CHECKS)


def test_schemathesis_failure_group_is_a_contract_discrepancy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Case:
        path = "/items"
        method = "GET"
        path_parameters: dict[str, object] = {}
        query: dict[str, object] = {}
        body = None

        def validate_response(self, _response: object, *, checks: list[object]) -> None:
            del checks
            raise FailureGroup([ServerError(operation="GET /items", status_code=500)])

    result = _single_case_runner(monkeypatch, Case()).test_operation(
        SimpleNamespace(path="/items", method="get")
    )

    assert result.status is _TestStatus.FAILED
    assert not result.errors
    assert len(result.discrepancies) == 1
    assert result.discrepancies[0].constraint_type == "schema_validation"


def test_unexpected_response_validator_exception_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Case:
        path = "/items"
        method = "GET"
        path_parameters = {"namespace": "example-namespace"}
        query: dict[str, object] = {}
        body = None

        def validate_response(self, _response: object, *, checks: list[object]) -> None:
            del checks
            raise RuntimeError("validator crashed in private-namespace")

    result = _single_case_runner(monkeypatch, Case()).test_operation(
        SimpleNamespace(path="/items", method="get")
    )

    assert result.status is _TestStatus.ERROR
    assert not result.discrepancies
    assert result.errors == [
        {
            "error": "validator crashed in <configured-namespace>",
            "stage": "response_validation",
            "case": {
                "path": "/items",
                "method": "GET",
                "path_parameters": {"namespace": "example-namespace"},
                "query": {},
                "body": None,
            },
        }
    ]
