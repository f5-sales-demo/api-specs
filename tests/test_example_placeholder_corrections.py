"""Guard the published specs against ambiguous organization placeholders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts import transform as transform_module
from scripts.transform import TransformConfig, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_SPECS_DIR = REPO_ROOT / "release" / "specs"
CONFIG_PATH = REPO_ROOT / "config" / "validation.yaml"
DOC_FIELDS = ("description", "summary", "title", "x-displayname", "x-ves-example")
PLACEHOLDER_NAME = "ac" + "me"
PROTOCOL_RECORD = "_" + PLACEHOLDER_NAME + "-challenge"


def _collect_documentation_strings(obj: Any, trail: str = "") -> list[tuple[str, str]]:
    """Return documentation-bearing string values with their JSON pointers."""
    found: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{trail}/{key}"
            if key in DOC_FIELDS and isinstance(value, str):
                found.append((child, value))
            found.extend(_collect_documentation_strings(value, child))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            found.extend(_collect_documentation_strings(value, f"{trail}/{index}"))
    return found


def test_published_specs_contain_no_ambiguous_organization_placeholders() -> None:
    """Only RFC 8555 protocol references may retain the overloaded name."""
    offenders: list[str] = []
    for spec_path in sorted(RELEASE_SPECS_DIR.glob("*.json")):
        spec = json.loads(spec_path.read_text())
        for pointer, value in _collect_documentation_strings(spec):
            lowered = value.lower()
            candidate = lowered.replace(PROTOCOL_RECORD, "")
            if "rfc 8555" in candidate:
                candidate = candidate.replace(PLACEHOLDER_NAME, "")
            if PLACEHOLDER_NAME in candidate:
                offenders.append(f"{spec_path.name}{pointer}")

    assert not offenders, (
        f"{len(offenders)} published documentation field(s) retain the ambiguous "
        "organization placeholder:\n" + "\n".join(offenders[:20])
    )


def test_published_specs_retain_all_rfc8555_challenge_records() -> None:
    """The sanitizer must not erase certificate-validation DNS record names."""
    references = 0
    for spec_path in sorted(RELEASE_SPECS_DIR.glob("*.json")):
        references += spec_path.read_text().lower().count(PROTOCOL_RECORD)

    assert references == 10


def test_placeholder_sanitizer_changes_only_configured_documentation_fields() -> None:
    """Configured examples change while protocol text and wire identifiers do not."""
    unsafe_tenant = PLACEHOLDER_NAME + "corp"
    safe_tenant = "example-corp"
    sanitizer = transform_module.sanitize_example_placeholders
    config = TransformConfig(
        metadata={
            "example_placeholder_corrections": {unsafe_tenant: safe_tenant},
            "example_placeholder_fields": DOC_FIELDS,
        }
    )
    spec = {
        "components": {
            "schemas": {
                unsafe_tenant: {
                    "description": f"Tenant {unsafe_tenant} uses {PROTOCOL_RECORD}.",
                    "x-ves-example": f"{unsafe_tenant}-web",
                    "properties": {
                        unsafe_tenant: {
                            "type": "string",
                            "enum": [unsafe_tenant],
                            "example": unsafe_tenant,
                        }
                    },
                }
            }
        }
    }

    result = sanitizer(spec, config, "test.json")
    schema = result["components"]["schemas"][unsafe_tenant]

    assert schema["description"] == f"Tenant {safe_tenant} uses {PROTOCOL_RECORD}."
    assert schema["x-ves-example"] == f"{safe_tenant}-web"
    assert unsafe_tenant in result["components"]["schemas"]
    assert unsafe_tenant in schema["properties"]
    assert schema["properties"][unsafe_tenant]["enum"] == [unsafe_tenant]
    assert schema["properties"][unsafe_tenant]["example"] == unsafe_tenant


def test_placeholder_correction_config_is_enabled_and_loaded() -> None:
    """The normal transform pipeline must consume the correction manifest."""
    config = load_config(CONFIG_PATH)
    unsafe_email = f"joe.doe@{PLACEHOLDER_NAME}.com"

    assert config.transforms["sanitize_example_placeholders"] is True
    assert config.metadata["example_placeholder_corrections"][unsafe_email] == "dana@example.com"
    assert tuple(config.metadata["example_placeholder_fields"]) == DOC_FIELDS
