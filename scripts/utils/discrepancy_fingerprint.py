"""Stable per-discrepancy fingerprint for tracking across runs."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .constraint_validator import Discrepancy


def fingerprint(d: Discrepancy, domain: str, method: str) -> str:
    """Return a 40-hex-char stable identifier for a discrepancy.

    Same inputs always yield the same fingerprint; any change to domain,
    method, path, property, or discrepancy type changes the fingerprint.
    """
    # ASCII unit separator (\x1f) cannot appear in OpenAPI paths, property
    # names, domain slugs, HTTP methods, or DiscrepancyType enum values, so
    # this delimiter is collision-free. "|" would be ambiguous if any field
    # ever contained a pipe character.
    parts = [
        domain,
        method.upper(),
        d.path,
        d.property_name,
        d.discrepancy_type.value,
    ]
    payload = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()


def short_form(fp: str) -> str:
    """First 8 hex characters, used as a queryable label."""
    return fp[:8]
