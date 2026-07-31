"""Boundary-aware replacements for selected OpenAPI text fields."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_SPELLING_TEXT_FIELDS: tuple[str, ...] = (
    "description",
    "summary",
    "title",
    "x-displayname",
)

# Exact placeholder corrections may include example values. Spelling fixes do
# not, because broad word corrections could corrupt example payload data.
DEFAULT_EXAMPLE_PLACEHOLDER_FIELDS: tuple[str, ...] = (
    *DEFAULT_SPELLING_TEXT_FIELDS,
    "x-ves-example",
)


def build_replacement_patterns(
    corrections: dict[str, str],
) -> list[tuple[re.Pattern[str], str]]:
    """Compile longer source values first with identifier-safe boundaries."""
    patterns = []
    for source in sorted(corrections, key=len, reverse=True):
        replacement = corrections[source]
        pattern = re.compile(r"(?<!\w)" + re.escape(source) + r"(?!\w)")
        patterns.append((pattern, replacement))
    return patterns


def replace_text_fields_recursive(
    obj: Any,
    patterns: list[tuple[re.Pattern[str], str]],
    text_fields: Sequence[str] = DEFAULT_SPELLING_TEXT_FIELDS,
) -> None:
    """Apply replacements to selected string fields without touching keys."""
    if isinstance(obj, dict):
        for key in text_fields:
            if key in obj and isinstance(obj[key], str):
                text = obj[key]
                for pattern, replacement in patterns:
                    text = pattern.sub(replacement, text)
                obj[key] = text
        for value in obj.values():
            replace_text_fields_recursive(value, patterns, text_fields)
    elif isinstance(obj, list):
        for item in obj:
            replace_text_fields_recursive(item, patterns, text_fields)
