"""Durable acknowledgement of exact downstream release-receipt delivery."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from typing import Any

import requests

from scripts.utils.strict_data import StrictDataError, canonical_json_bytes, strict_json_loads

GITHUB_API_VERSION = "2022-11-28"
DELIVERY_ENVIRONMENT = "api-specs-enriched-release-delivery"
RECEIPT_FIELDS = frozenset(
    {"version", "tag_name", "published_at", "asset_name", "asset_size", "asset_digest"}
)
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
RELEASE_VERSION_PATTERN = re.compile(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}-[1-9][0-9]*")


class DeliveryAckError(RuntimeError):
    """Raised when durable delivery state cannot be proved safely."""


def _validate_receipt(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS:
        raise DeliveryAckError("release receipt must contain exactly six fields")
    version = receipt["version"]
    if not isinstance(version, str) or RELEASE_VERSION_PATTERN.fullmatch(version) is None:
        raise DeliveryAckError("release receipt version is invalid")
    if receipt["tag_name"] != f"v{version}":
        raise DeliveryAckError("release receipt tag does not match version")
    published = receipt["published_at"]
    if (
        not isinstance(published, str)
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
            published,
        )
        is None
    ):
        raise DeliveryAckError("release receipt publication timestamp is invalid")
    if receipt["asset_name"] != f"api-specs-v{version}.zip":
        raise DeliveryAckError("release receipt asset name does not match version")
    size = receipt["asset_size"]
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise DeliveryAckError("release receipt asset size is invalid")
    digest = receipt["asset_digest"]
    if not isinstance(digest, str) or DIGEST_PATTERN.fullmatch(digest) is None:
        raise DeliveryAckError("release receipt asset digest is invalid")
    return receipt


def receipt_digest(receipt: dict[str, Any]) -> str:
    """Return a canonical SHA-256 identity for the exact six-field receipt."""
    validated = _validate_receipt(receipt)
    canonical = canonical_json_bytes(validated)
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _validate_identity(repository: str, commit: str, token: str) -> None:
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise DeliveryAckError("repository must be an owner/name pair")
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise DeliveryAckError("release commit must be a full Git SHA")
    if not token:
        raise DeliveryAckError("GitHub token is required")


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


def _response_json(response: requests.Response, description: str) -> Any:
    try:
        response.raise_for_status()
    except requests.RequestException as error:
        raise DeliveryAckError(f"{description} failed with HTTP {response.status_code}") from error
    try:
        return response.json()
    except (requests.JSONDecodeError, ValueError) as error:
        raise DeliveryAckError(f"{description} returned invalid JSON") from error


def _get_all_pages(
    url: str,
    token: str,
    params: dict[str, str],
    description: str,
) -> list[Any]:
    entries: list[Any] = []
    page = 1
    while True:
        response = requests.get(
            url,
            headers=_headers(token),
            params={**params, "per_page": "100", "page": str(page)},
            timeout=30,
        )
        payload = _response_json(response, description)
        if not isinstance(payload, list):
            raise DeliveryAckError(f"{description} did not return an array")
        entries.extend(payload)
        if len(payload) < 100:
            return entries
        page += 1


def _payload_digest(payload: Any) -> str | None:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "receipt_digest"}
        or payload.get("schema_version") != 1
    ):
        return None
    digest = payload.get("receipt_digest")
    if not isinstance(digest, str) or DIGEST_PATTERN.fullmatch(digest) is None:
        return None
    return digest


def get_delivery_ack(
    repository: str,
    commit: str,
    receipt: dict[str, Any],
    token: str,
) -> bool:
    """Return whether an exact successful deployment acknowledges the receipt."""
    _validate_identity(repository, commit, token)
    expected_digest = receipt_digest(receipt)
    deployments = _get_all_pages(
        f"https://api.github.com/repos/{repository}/deployments",
        token,
        {
            "sha": commit,
            "environment": DELIVERY_ENVIRONMENT,
        },
        "delivery acknowledgement lookup",
    )
    acknowledged = False
    for index, deployment in enumerate(deployments):
        if not isinstance(deployment, dict):
            raise DeliveryAckError(f"delivery acknowledgement deployment[{index}] is invalid")
        if deployment.get("sha") != commit or deployment.get("environment") != DELIVERY_ENVIRONMENT:
            raise DeliveryAckError("delivery acknowledgement lookup returned conflicting identity")
        if _payload_digest(deployment.get("payload")) != expected_digest:
            continue
        deployment_id = deployment.get("id")
        if (
            isinstance(deployment_id, bool)
            or not isinstance(deployment_id, int)
            or deployment_id <= 0
        ):
            raise DeliveryAckError("delivery acknowledgement deployment id is invalid")
        statuses = _get_all_pages(
            f"https://api.github.com/repos/{repository}/deployments/{deployment_id}/statuses",
            token,
            {},
            "delivery acknowledgement status lookup",
        )
        if not all(isinstance(status, dict) for status in statuses):
            raise DeliveryAckError("delivery acknowledgement statuses are malformed")
        states = [status.get("state") for status in statuses]
        if any(not isinstance(state, str) for state in states):
            raise DeliveryAckError("delivery acknowledgement status state is malformed")
        if "success" in states:
            acknowledged = True
    return acknowledged


def record_delivery_ack(
    repository: str,
    commit: str,
    receipt: dict[str, Any],
    token: str,
) -> None:
    """Record success after dispatch, idempotently for one exact receipt."""
    _validate_identity(repository, commit, token)
    digest = receipt_digest(receipt)
    if get_delivery_ack(repository, commit, receipt, token):
        return
    deployment_response = requests.post(
        f"https://api.github.com/repos/{repository}/deployments",
        headers=_headers(token),
        json={
            "ref": commit,
            "environment": DELIVERY_ENVIRONMENT,
            "description": "Exact api-specs-enriched release receipt delivered",
            "auto_merge": False,
            "required_contexts": [],
            "payload": {"schema_version": 1, "receipt_digest": digest},
        },
        timeout=30,
    )
    deployment = _response_json(deployment_response, "delivery acknowledgement creation")
    if not isinstance(deployment, dict):
        raise DeliveryAckError("delivery acknowledgement creation returned an invalid object")
    deployment_id = deployment.get("id")
    if isinstance(deployment_id, bool) or not isinstance(deployment_id, int) or deployment_id <= 0:
        raise DeliveryAckError("delivery acknowledgement creation returned no deployment id")
    status_response = requests.post(
        f"https://api.github.com/repos/{repository}/deployments/{deployment_id}/statuses",
        headers=_headers(token),
        json={
            "state": "success",
            "environment": DELIVERY_ENVIRONMENT,
            "description": "Downstream repository dispatch accepted",
        },
        timeout=30,
    )
    _response_json(status_response, "delivery acknowledgement status creation")


def main() -> int:
    """Record an acknowledgement from workflow-provided exact identity."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record")
    record.add_argument("--repository", required=True)
    record.add_argument("--commit", required=True)
    record.add_argument("--receipt-json", required=True)
    record.add_argument("--token-env", default="ACK_TOKEN")
    args = parser.parse_args()
    try:
        receipt = strict_json_loads(args.receipt_json, "release receipt")
        if not isinstance(receipt, dict):
            raise DeliveryAckError("release receipt must be a JSON object")
        token = os.environ.get(args.token_env, "")
        record_delivery_ack(args.repository, args.commit, receipt, token)
    except (StrictDataError, DeliveryAckError, requests.RequestException) as error:
        print(f"Delivery acknowledgement failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
