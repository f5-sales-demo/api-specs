"""Guard tests for the spelling-correction contract.

Two invariants are enforced here:

1. Every key in ``config/spelling_corrections.yaml``, and every case variant of
   one, is actually gone from the published ``release/specs`` artifact. The
   corrections were configured and enabled long before this test existed, yet
   the committed artifact still shipped dozens of them downstream because
   nothing ever proved the artifact matched the configuration.
2. No correction key can match an identifier. ``blocked_sevice`` and
   ``checkin`` are real F5 XC wire keys; a blind text replacement that
   "corrects" them breaks the API contract.

This module deliberately contains no misspelled literals: the words under test
are read from the configuration and from the specs at run time.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.transform import (
    DEFAULT_SPELLING_TEXT_FIELDS,
    TransformConfig,
    _build_spelling_patterns,
    fix_property_names,
    fix_spelling,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_SPECS_DIR = REPO_ROOT / "release" / "specs"
SPELLING_CONFIG = REPO_ROOT / "config" / "spelling_corrections.yaml"
PROPERTY_CONFIG = REPO_ROOT / "config" / "property_name_corrections.yaml"

# Property names the live F5 XC API requires despite the misspelling. They must
# survive every pipeline stage untouched.
WIRE_CONTRACT_PROPERTY_NAMES = ("blocked_sevice", "checkin")

# Maximum number of offending samples quoted in an assertion message.
_MAX_REPORTED = 20


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def spelling_config() -> dict:
    with SPELLING_CONFIG.open() as fh:
        return yaml.safe_load(fh) or {}


@pytest.fixture(scope="module")
def corrections(spelling_config: dict) -> dict[str, str]:
    return spelling_config.get("corrections", {})


@pytest.fixture(scope="module")
def text_fields(spelling_config: dict) -> tuple[str, ...]:
    return tuple(spelling_config.get("text_fields") or DEFAULT_SPELLING_TEXT_FIELDS)


@pytest.fixture(scope="module")
def patterns(corrections: dict[str, str]) -> list[tuple[re.Pattern[str], str]]:
    return _build_spelling_patterns(corrections)


@pytest.fixture(scope="module")
def released_specs() -> list[tuple[str, dict]]:
    """Load every published spec. Fails loudly if the artifact is missing."""
    spec_files = sorted(RELEASE_SPECS_DIR.glob("*.json"))
    assert spec_files, f"No published specs found in {RELEASE_SPECS_DIR}"
    specs = []
    for spec_file in spec_files:
        with spec_file.open() as fh:
            specs.append((spec_file.name, json.load(fh)))
    return specs


def _collect_prose(obj: Any, fields: tuple[str, ...], trail: str) -> list[tuple[str, str]]:
    """Return ``(json_pointer, value)`` for every prose field in *obj*."""
    found: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{trail}/{key}"
            if key in fields and isinstance(value, str):
                found.append((child, value))
            found.extend(_collect_prose(value, fields, child))
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            found.extend(_collect_prose(item, fields, f"{trail}/{index}"))
    return found


def _collect_identifiers(obj: Any) -> set[str]:
    """Return every identifier-like token in *obj*.

    Covers schema property names, ``required`` entries, ``enum`` values,
    parameter names, component names, operation ids and path templates -- the
    tokens that carry meaning on the wire and must never be spell-corrected.
    """
    names: set[str] = set()
    if isinstance(obj, dict):
        properties = obj.get("properties")
        if isinstance(properties, dict):
            names.update(k for k in properties if isinstance(k, str))
        for list_key in ("required", "enum"):
            values = obj.get(list_key)
            if isinstance(values, list):
                names.update(v for v in values if isinstance(v, str))
        if isinstance(obj.get("in"), str) and isinstance(obj.get("name"), str):
            names.add(obj["name"])
        if isinstance(obj.get("operationId"), str):
            names.add(obj["operationId"])
        for section in ("schemas", "paths", "securitySchemes"):
            entries = obj.get(section)
            if isinstance(entries, dict):
                names.update(k for k in entries if isinstance(k, str))
        for value in obj.values():
            names.update(_collect_identifiers(value))
    elif isinstance(obj, list):
        for item in obj:
            names.update(_collect_identifiers(item))
    return names


def _property_names(obj: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(obj, dict):
        properties = obj.get("properties")
        if isinstance(properties, dict):
            names.update(k for k in properties if isinstance(k, str))
        for value in obj.values():
            names.update(_property_names(value))
    elif isinstance(obj, list):
        for item in obj:
            names.update(_property_names(item))
    return names


# ---------------------------------------------------------------------------
# Invariant 1 -- the published artifact matches the configuration
# ---------------------------------------------------------------------------


def test_release_specs_contain_no_configured_typos(
    released_specs: list[tuple[str, dict]],
    patterns: list[tuple[re.Pattern[str], str]],
    text_fields: tuple[str, ...],
) -> None:
    """No ``spelling_corrections.yaml`` key survives in the published specs."""
    offenders: list[str] = []
    for filename, spec in released_specs:
        for pointer, value in _collect_prose(spec, text_fields, ""):
            for pattern, _fix in patterns:
                if pattern.search(value):
                    offenders.append(f"{filename}{pointer}: {pattern.pattern}")
                    break

    assert not offenders, (
        f"{len(offenders)} prose field(s) in release/specs still contain a "
        f"configured spelling correction -- the published artifact is stale or "
        f"the transform did not run:\n" + "\n".join(offenders[:_MAX_REPORTED])
    )


def test_text_fields_exclude_data_fields(text_fields: tuple[str, ...]) -> None:
    """Prose-only: example/data fields are never spell-corrected."""
    assert text_fields, "text_fields must declare at least one prose field"
    assert "x-ves-example" not in text_fields, (
        "x-ves-example holds wire values (for example 'Operational', 'ns1'), "
        "not prose -- correcting it can corrupt example payloads"
    )


def test_case_variants_of_corrections_are_configured(
    released_specs: list[tuple[str, dict]],
    corrections: dict[str, str],
    text_fields: tuple[str, ...],
) -> None:
    """A configured typo must not survive under a different capitalisation.

    Corrections are case-sensitive, so a lower-case entry leaves the
    capitalised spelling in place. That is how the capitalised singular of an
    already-configured plural kept shipping.
    """
    variants: dict[str, re.Pattern[str]] = {}
    for typo in corrections:
        for variant in (typo.lower(), typo.upper(), typo.capitalize()):
            if variant != typo and variant not in corrections:
                variants[variant] = re.compile(r"(?<!\w)" + re.escape(variant) + r"(?!\w)")

    offenders: list[str] = []
    for filename, spec in released_specs:
        for pointer, value in _collect_prose(spec, text_fields, ""):
            for variant, pattern in variants.items():
                if pattern.search(value):
                    offenders.append(f"{filename}{pointer}: {variant}")

    assert not offenders, (
        f"{len(offenders)} prose field(s) contain an unconfigured case variant "
        f"of a configured correction -- add the variant to "
        f"config/spelling_corrections.yaml:\n" + "\n".join(sorted(set(offenders))[:_MAX_REPORTED])
    )


# ---------------------------------------------------------------------------
# Invariant 2 -- corrections can never touch an identifier
# ---------------------------------------------------------------------------


def test_corrections_never_match_an_identifier(
    released_specs: list[tuple[str, dict]],
    patterns: list[tuple[re.Pattern[str], str]],
) -> None:
    """No correction key matches a property name, enum value or other token."""
    identifiers: set[str] = set()
    for _filename, spec in released_specs:
        identifiers.update(_collect_identifiers(spec))

    collisions: list[str] = []
    for identifier in sorted(identifiers):
        for pattern, fix in patterns:
            if pattern.search(identifier):
                collisions.append(f"{pattern.pattern} -> {fix} matches {identifier!r}")

    assert not collisions, (
        "Spelling corrections would rewrite API identifiers -- this is the "
        "failure mode that broke blocked_sevice/checkin downstream:\n"
        + "\n".join(collisions[:_MAX_REPORTED])
    )


def test_wire_contract_property_names_are_preserved(
    released_specs: list[tuple[str, dict]],
) -> None:
    """``blocked_sevice`` and ``checkin`` still exist as property names."""
    names: set[str] = set()
    for _filename, spec in released_specs:
        names.update(_property_names(spec))

    for wire_key in WIRE_CONTRACT_PROPERTY_NAMES:
        assert wire_key in names, (
            f"{wire_key!r} disappeared from the published specs. The live API "
            f"requires this exact spelling; renaming it breaks the wire contract."
        )


def test_blocked_sevice_rename_is_marked_never_apply() -> None:
    """The rename that breaks the wire contract stays permanently unapplied."""
    with PROPERTY_CONFIG.open() as fh:
        cfg = yaml.safe_load(fh) or {}

    rules = [c for c in cfg.get("corrections", []) if c["old_key"] == "blocked_sevice"]
    assert len(rules) == 1, "expected exactly one blocked_sevice rule"
    rule = rules[0]

    assert rule["never_apply"] is True
    assert rule["verified"] is False
    assert rule["api_status"] == "upstream_typo_permanent"
    assert "NEVER APPLY" in rule["notes"]


def test_never_apply_overrides_verified() -> None:
    """``never_apply`` wins even if a rule is somehow marked verified."""
    spec = {
        "components": {
            "schemas": {
                "fleetBlockedServicesListType": {
                    "properties": {"blocked_sevice": {"type": "object"}},
                    "required": ["blocked_sevice"],
                }
            }
        }
    }
    config = TransformConfig(
        metadata={
            "property_name_corrections": [
                {
                    "schema": "fleetBlockedServicesListType",
                    "old_key": "blocked_sevice",
                    "new_key": "blocked_service",
                    "verified": True,
                    "never_apply": True,
                }
            ]
        }
    )

    result = fix_property_names(spec, config, "test.json")
    schema = result["components"]["schemas"]["fleetBlockedServicesListType"]
    assert "blocked_sevice" in schema["properties"]
    assert "blocked_service" not in schema["properties"]
    assert schema["required"] == ["blocked_sevice"]


# ---------------------------------------------------------------------------
# Transform behaviour
# ---------------------------------------------------------------------------


def test_fix_spelling_rewrites_prose_and_leaves_data_alone() -> None:
    """Prose fields are corrected; schema keys and data fields are not.

    Uses a synthetic token rather than a real misspelling so the file itself
    stays free of typos.
    """
    typo, fix = "Wibbel", "Wibble"
    config = TransformConfig(
        metadata={
            "spelling_corrections": {typo: fix},
            "spelling_text_fields": ("description", "x-displayname"),
        }
    )
    spec = {
        "components": {
            "schemas": {
                typo: {
                    "description": f"{typo} prefix for the host",
                    "x-displayname": f"{typo} Settings",
                    "x-ves-example": typo,
                    "properties": {typo: {"type": "string"}},
                    "enum": [typo],
                }
            }
        }
    }

    result = fix_spelling(spec, config, "test.json")
    schema = result["components"]["schemas"][typo]

    assert schema["description"] == f"{fix} prefix for the host"
    assert schema["x-displayname"] == f"{fix} Settings"
    assert schema["x-ves-example"] == typo
    assert typo in schema["properties"]
    assert schema["enum"] == [typo]
    assert typo in result["components"]["schemas"]


def test_fix_spelling_respects_word_boundaries() -> None:
    """A correction never fires inside a longer identifier-like token."""
    config = TransformConfig(
        metadata={
            "spelling_corrections": {"sevice": "service", "checkin": "checking"},
            "spelling_text_fields": ("description",),
        }
    )
    spec = {"description": "The blocked_sevice field and site_checkin timer."}

    result = fix_spelling(spec, config, "test.json")
    assert result["description"] == "The blocked_sevice field and site_checkin timer."
