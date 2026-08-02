"""Fail-closed authentication and namespace-probe contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.utils.auth import F5XCAuth, load_auth_from_config


def test_live_auth_requires_url_token_and_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("F5XC_API_URL", raising=False)
    monkeypatch.setenv("F5XC_API_TOKEN", "not-a-real-token")
    monkeypatch.delenv("F5XC_NAMESPACE", raising=False)

    with pytest.raises(ValueError, match="F5XC_API_URL, F5XC_NAMESPACE"):
        load_auth_from_config({"api": {}})


def test_environment_is_the_only_live_target_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("F5XC_API_URL", "https://environment.example.invalid")
    monkeypatch.setenv("F5XC_API_TOKEN", "not-a-real-token")
    monkeypatch.setenv("F5XC_NAMESPACE", "environment-namespace")

    auth = load_auth_from_config(
        {
            "api": {
                "base_url": "https://stale-config.example.invalid",
                "namespace": "example-namespace",
            }
        }
    )

    assert auth.api_url == "https://environment.example.invalid"
    assert auth.namespace =example-namespace"environment-namespace"


def test_connection_fails_when_configured_namespace_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = F5XCAuth(
        api_url="https://example.invalid",
        api_token="not-a-real-token",
        namespace="example-namespace",
    )
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {"items": [{"name": "different-namespace"}]},
    )
    monkeypatch.setattr(auth, "get", lambda _path: response)

    assert auth.test_connection() is False


def test_connection_accepts_only_the_configured_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = F5XCAuth(
        api_url="https://example.invalid",
        api_token="not-a-real-token",
        namespace="example-namespace",
    )
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {"items": [{"name": "required-namespace"}]},
    )
    monkeypatch.setattr(auth, "get", lambda _path: response)

    assert auth.test_connection() is True
