"""Live-API re-probe for a discrepancy, used by issue_sync.py."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .constraint_validator import DiscrepancyType

if TYPE_CHECKING:
    import httpx

    from .constraint_validator import Discrepancy

_BODY_LIMIT = 2048
_HTTP_OK_MIN = 200
_HTTP_OK_MAX_EXCLUSIVE = 300


@dataclass
class ReprobeEvidence:
    """Evidence captured from a single live-API re-probe request.

    Attributes:
        endpoint_url: Full URL that was requested.
        method: HTTP method used (uppercased).
        test_value: The value injected into the payload for the discrepancy property.
        status_code: HTTP status code returned by the live API.
        body_snippet: Truncated response body (up to ``_BODY_LIMIT`` characters).
        timestamp_utc: ISO-8601 timestamp of the probe, suffixed with ``Z``.
        discrepancy_still_present: Whether the re-probe confirms the original discrepancy.
    """

    endpoint_url: str
    method: str
    test_value: object
    status_code: int
    body_snippet: str
    timestamp_utc: str
    discrepancy_still_present: bool


def reprobe_discrepancy(
    d: Discrepancy,
    domain: str,
    method: str,
    client: httpx.Client,
) -> ReprobeEvidence:
    """Issue a single request that exercises the discrepancy and capture evidence.

    Uses the first test_value recorded by reconcile as the payload seed.

    Args:
        d: The discrepancy to re-probe.
        domain: The resource domain (e.g. ``"origin_pool"``) used for logging context.
        method: The HTTP method to use for the probe (e.g. ``"POST"``).
        client: An ``httpx.Client`` bound to the target API base URL. Injected so
            callers can supply a ``MockTransport`` in unit tests.

    Returns:
        A ``ReprobeEvidence`` instance describing the probe outcome.
    """
    del domain  # reserved for future per-domain dispatch; keep signature stable
    test_value = d.test_values[0] if d.test_values else None
    payload = {d.property_name: test_value} if test_value is not None else {}
    resp = client.request(method, d.path, json=payload)

    body = resp.text[:_BODY_LIMIT]

    accepted = _HTTP_OK_MIN <= resp.status_code < _HTTP_OK_MAX_EXCLUSIVE
    # SPEC_STRICTER: spec rejects a value the API accepts. Discrepancy still
    # present iff API keeps accepting that value; resolved when API now rejects
    # (i.e. upstream has tightened to match the spec).
    #
    # SPEC_LOOSER: spec accepts a value the API rejects. Discrepancy still
    # present iff API keeps rejecting; resolved when API now accepts (upstream
    # has loosened to match the spec).
    #
    # Other DiscrepancyType values (MISSING/EXTRA/CONSTRAINT_MISMATCH/
    # TYPE_MISMATCH) lack an unambiguous 2xx-vs-4xx signal — default to "still
    # present" and let the do-not-auto-close label or human review decide.
    if d.discrepancy_type == DiscrepancyType.SPEC_STRICTER:
        still_present = accepted
    elif d.discrepancy_type == DiscrepancyType.SPEC_LOOSER:
        still_present = not accepted
    else:
        still_present = True

    return ReprobeEvidence(
        endpoint_url=str(resp.request.url),
        method=method.upper(),
        test_value=test_value,
        status_code=resp.status_code,
        body_snippet=body,
        timestamp_utc=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        discrepancy_still_present=still_present,
    )
