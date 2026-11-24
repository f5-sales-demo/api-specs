"""Guard the published specs against ambiguous organization placeholders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

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


def _configured_placeholders(value: str, config: TransformConfig) -> list[str]:
    """Reject exact configured fictitious examples, not an overloaded protocol name."""
    return [
        placeholder
        for placeholder in config.metadata["example_placeholder_corrections"]
        if placeholder in value
    ]


def test_published_specs_contain_no_configured_organization_placeholders() -> None:
    config = load_config(CONFIG_PATH)
    offenders: list[str] = []
    for spec_path in sorted(RELEASE_SPECS_DIR.glob("*.json")):
        spec = json.loads(spec_path.read_text())
        for pointer, value in _collect_documentation_strings(spec):
            if _configured_placeholders(value, config):
                offenders.append(f"{spec_path.name}{pointer}")

    assert not offenders, (
        f"{len(offenders)} published documentation field(s) retain configured "
        "organization placeholders:\n" + "\n".join(offenders[:20])
    )


@pytest.mark.parametrize("field", DOC_FIELDS)
def test_protocol_terminology_and_urls_are_preserved(field: str) -> None:
    config = load_config(CONFIG_PATH)
    text = (
        "ACME client, ACME server, ACME certificate authority and AcmeChallenge resource; "
        "https://acme-v02.api.letsencrypt.org/directory; "
        "https://acme-staging-v02.api.letsencrypt.org/directory; "
        f"{PROTOCOL_RECORD}.example.com"
    )
    assert not _configured_placeholders(text, config)
    result = transform_module.sanitize_example_placeholders({field: text}, config, "test.json")
    assert result[field] == text


@pytest.mark.parametrize("field", DOC_FIELDS)
def test_every_configured_placeholder_is_detected_and_corrected(field: str) -> None:
    config = load_config(CONFIG_PATH)
    for unsafe, safe in config.metadata["example_placeholder_corrections"].items():
        text = f"Example: {unsafe}."
        assert unsafe in _configured_placeholders(text, config)
        result = transform_module.sanitize_example_placeholders({field: text}, config, "test.json")
        assert result[field] == f"Example: {safe}."
        assert not _configured_placeholders(result[field], config)


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
        "paths": {f"/{unsafe_tenant}": {"get": {"operationId": unsafe_tenant}}},
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
        },
    }

    result = sanitizer(spec, config, "test.json")
    schema = result["components"]["schemas"][unsafe_tenant]

    assert schema["description"] == f"Tenant {safe_tenant} uses {PROTOCOL_RECORD}."
    assert schema["x-ves-example"] == f"{safe_tenant}-web"
    assert unsafe_tenant in result["components"]["schemas"]
    assert unsafe_tenant in schema["properties"]
    assert schema["properties"][unsafe_tenant]["enum"] == [unsafe_tenant]
    assert schema["properties"][unsafe_tenant]["example"] == unsafe_tenant
    assert result["paths"][f"/{unsafe_tenant}"]["get"]["operationId"] == unsafe_tenant


def test_placeholder_correction_config_is_enabled_and_loaded() -> None:
    """The normal transform pipeline must consume the correction manifest."""
    config = load_config(CONFIG_PATH)
    unsafe_email = f"joe.doe@{PLACEHOLDER_NAME}.com"

    assert config.transforms["sanitize_example_placeholders"] is True
    assert config.metadata["example_placeholder_corrections"][unsafe_email] == "dana@example.com"
    assert tuple(config.metadata["example_placeholder_fields"]) == DOC_FIELDS
