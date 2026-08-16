"""Deterministic replacement of unsafe PII-shaped documentation examples."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .check_pii import EMAIL_RE, IDENTITY_FIELD_RE, PERSON_FIELD_RE, placeholder_value, safe_email


def _replace_structured_literals(
    text: str,
    pattern: re.Pattern[str],
    replacement_for_key: Callable[[str], str],
) -> str:
    """Replace scalar documentation values while preserving their field syntax."""
    pieces: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        raw_value = match.group("value")
        value = re.split(r"(?=[,;])", raw_value, maxsplit=1)[0]
        if not value or value.strip().isdigit() or placeholder_value(value):
            continue
        value_start = match.start("value")
        value_end = value_start + len(value)
        pieces.extend((text[cursor:value_start], replacement_for_key(match.group("key").lower())))
        cursor = value_end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _sanitize_pii_text(text: str) -> str:
    """Replace contact and identity literals with documented synthetic values."""

    def replace_email(match: re.Match[str]) -> str:
        return match.group(0) if safe_email(match.group(0)) else "dana@example.com"

    text = EMAIL_RE.sub(replace_email, text)
    text = _replace_structured_literals(text, PERSON_FIELD_RE, lambda _key: "Dana R.")

    def identity_placeholder(key: str) -> str:
        category = next(
            (
                candidate
                for candidate in (
                    "tenant",
                    "customer",
                    "account",
                    "subscription",
                    "project",
                    "namespace",
                )
                if candidate in key
            ),
            "resource",
        )
        return f"example-{category}"

    return _replace_structured_literals(text, IDENTITY_FIELD_RE, identity_placeholder)


def sanitize_pii_strings(value: Any) -> Any:
    """Return *value* with every nested string sanitized deterministically."""
    if isinstance(value, dict):
        return {key: sanitize_pii_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_pii_strings(item) for item in value]
    return _sanitize_pii_text(value) if isinstance(value, str) else value
