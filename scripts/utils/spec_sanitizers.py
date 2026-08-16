"""Reusable recursive sanitizers for OpenAPI objects."""

from __future__ import annotations

import re
from typing import Any


def strip_scripts_recursive(obj: Any) -> None:
    """Strip script tags from nested OpenAPI description fields in place."""
    if isinstance(obj, dict):
        if "description" in obj and isinstance(obj["description"], str):
            obj["description"] = re.sub(
                r"<script[^>]*>.*?</script>",
                "",
                obj["description"],
                flags=re.DOTALL | re.IGNORECASE,
            )
            obj["description"] = re.sub(
                r"</?script[^>]*>",
                "",
                obj["description"],
                flags=re.IGNORECASE,
            )
        for value in obj.values():
            strip_scripts_recursive(value)
    elif isinstance(obj, list):
        for item in obj:
            strip_scripts_recursive(item)
