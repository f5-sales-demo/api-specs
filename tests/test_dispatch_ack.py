"""Durable exact-receipt downstream delivery acknowledgements."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import requests

from scripts.dispatch_ack import (
    DeliveryAckError,
    get_delivery_ack,
    receipt_digest,
    record_delivery_ack,
)

RECEIPT = {
    "version": "2026.08.02-1",
    "tag_name": "v2026.08.02-1",
    "published_at": "2026-08-02T08:25:00Z",
    "asset_name": "api-specs-v2026.08.02-1.zip",
    "asset_size": 123,
    "asset_digest": "sha256:" + "a" * 64,
}
COMMIT = "b" * 40


@dataclass
class _Response:
    payload: object
    status_code: int = 200

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self) -> object:
        return self.payload


def _deployment(payload: object) -> dict:
    return {
        "id": 7,
        "sha": COMMIT,
        "environment": "api-specs-enriched-release-delivery",
        "payload": payload,
    }


def test_receipt_digest_is_order_independent_and_shape_strict() -> None:
    assert receipt_digest(RECEIPT) == receipt_digest(dict(reversed(list(RECEIPT.items()))))

    malformed = dict(RECEIPT)
    malformed["unexpected"] = True
    with pytest.raises(DeliveryAckError, match="exactly six fields"):
        receipt_digest(malformed)


def test_exact_successful_deployment_acknowledges_receipt(monkeypatch) -> None:
    digest = receipt_digest(RECEIPT)
    responses = iter(
        [
            _Response([_deployment({"schema_version": 1, "receipt_digest": digest})]),
            _Response([{"state": "success"}]),
        ]
    )
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: next(responses))

    assert get_delivery_ack("example/api-specs", COMMIT, RECEIPT, "token") is True


def test_other_valid_receipt_on_same_commit_does_not_conflict(monkeypatch) -> None:
    exact_digest = receipt_digest(RECEIPT)
    other_receipt = dict(RECEIPT)
    other_receipt["version"] = "2026.08.02-2"
    other_receipt["tag_name"] = "v2026.08.02-2"
    other_receipt["asset_name"] = "api-specs-v2026.08.02-2.zip"
    other = _deployment({"schema_version": 1, "receipt_digest": receipt_digest(other_receipt)})
    other["id"] = 6
    exact = _deployment({"schema_version": 1, "receipt_digest": exact_digest})
    responses = iter([_Response([other, exact]), _Response([{"state": "success"}])])
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: next(responses))

    assert get_delivery_ack("example/api-specs", COMMIT, RECEIPT, "token") is True


def test_ack_lookup_paginates_deployments_and_exact_statuses(monkeypatch) -> None:
    exact_digest = receipt_digest(RECEIPT)
    other_digest = "sha256:" + "c" * 64
    first_page = []
    for deployment_id in range(1, 101):
        deployment = _deployment({"schema_version": 1, "receipt_digest": other_digest})
        deployment["id"] = deployment_id
        first_page.append(deployment)
    exact = _deployment({"schema_version": 1, "receipt_digest": exact_digest})

    def get(url: str, **kwargs) -> _Response:
        page = kwargs["params"]["page"]
        if url.endswith("/deployments"):
            return _Response(first_page if page == "1" else [exact])
        if url.endswith("/deployments/7/statuses"):
            return _Response(
                [{"state": "pending"}] * 100 if page == "1" else [{"state": "success"}]
            )
        raise AssertionError(f"unexpected acknowledgement URL: {url}")

    monkeypatch.setattr(requests, "get", get)

    assert get_delivery_ack("example/api-specs", COMMIT, RECEIPT, "token") is True


def test_missing_ack_is_recoverable_but_malformed_or_conflicting_ack_fails(monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: _Response([]))
    assert get_delivery_ack("example/api-specs", COMMIT, RECEIPT, "token") is False

    for payload in ({"schema_version": 1}, {"schema_version": 1, "receipt_digest": "bad"}):
        monkeypatch.setattr(
            requests,
            "get",
            lambda *_args, payload=payload, **_kwargs: _Response([_deployment(payload)]),
        )
        with pytest.raises(DeliveryAckError, match="acknowledgement payload"):
            get_delivery_ack("example/api-specs", COMMIT, RECEIPT, "token")


def test_ack_lookup_error_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *_args, **_kwargs: _Response({}, status_code=403),
    )

    with pytest.raises(DeliveryAckError, match="HTTP 403"):
        get_delivery_ack("example/api-specs", COMMIT, RECEIPT, "token")


def test_record_ack_creates_deployment_then_success_status(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "scripts.dispatch_ack.get_delivery_ack",
        lambda *_args, **_kwargs: False,
    )

    def post(url: str, **kwargs) -> _Response:
        calls.append((url, kwargs["json"]))
        if url.endswith("/deployments"):
            return _Response({"id": 42}, status_code=201)
        return _Response({}, status_code=201)

    monkeypatch.setattr(requests, "post", post)

    record_delivery_ack("example/api-specs", COMMIT, RECEIPT, "token")

    assert calls[0][0].endswith("/deployments")
    assert calls[0][1]["ref"] == COMMIT
    assert calls[0][1]["payload"]["receipt_digest"] == receipt_digest(RECEIPT)
    assert calls[1][0].endswith("/deployments/42/statuses")
    assert calls[1][1]["state"] == "success"


def test_record_ack_is_idempotent(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.dispatch_ack.get_delivery_ack",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail("idempotent ack must not write"),
    )

    record_delivery_ack("example/api-specs", COMMIT, RECEIPT, "token")
