"""Acceptance tests for property name corrections against the released specs.

These run the ``fix_property_names`` transform over the real
``config/property_name_corrections.yaml`` and the real ``release/specs``
files, and assert both halves of the contract for every tracked correction:

* the **presented** name is corrected, everywhere the specs name it, and
* the **wire** key is byte-identical to what F5's platform accepts today.

No misspelled key is written out in this module; every one is read from the
config, which is the single source of truth.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.transform import (
    FIX_SPEC_STATUS,
    ONEOF_FIELD_PREFIX,
    WIRE_NAME_EXTENSION,
    TransformConfig,
    fix_property_names,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config" / "property_name_corrections.yaml"
SPECS_DIR = REPO_ROOT / "release" / "specs"


def _load_corrections() -> list[dict]:
    with CONFIG_PATH.open() as fh:
        return (yaml.safe_load(fh) or {}).get("corrections", [])


CORRECTIONS = _load_corrections()
CORRECTION_IDS = [c["old_key"] for c in CORRECTIONS]


def _changes_the_wire(correction: dict) -> bool:
    """True when the rename is also a wire change (the API uses the fix)."""
    return correction.get("verified", False) and correction.get("api_status") == FIX_SPEC_STATUS


def _affected_spec_paths() -> list[Path]:
    """Return the released specs that mention at least one tracked typo."""
    tokens = [f'"{c["old_key"]}"' for c in CORRECTIONS]
    return [
        path
        for path in sorted(SPECS_DIR.glob("*.json"))
        if any(token in path.read_text() for token in tokens)
    ]


@pytest.fixture(scope="module")
def transformed_specs() -> list[tuple[str, dict, dict]]:
    """Return ``(filename, original, transformed)`` for every affected spec."""
    config = TransformConfig(metadata={"property_name_corrections": CORRECTIONS})
    results = []
    for path in _affected_spec_paths():
        with path.open() as fh:
            original = json.load(fh)
        transformed = fix_property_names(copy.deepcopy(original), config, path.name)
        results.append((path.name, original, transformed))
    return results


def _schemas(spec: dict) -> dict:
    return spec.get("components", {}).get("schemas", {})


def _wire_keys(schema_def: dict) -> set[str]:
    """Return the JSON keys a schema puts on the wire."""
    keys = set()
    for name, prop in schema_def.get("properties", {}).items():
        if isinstance(prop, dict) and WIRE_NAME_EXTENSION in prop:
            keys.add(prop[WIRE_NAME_EXTENSION])
        else:
            keys.add(name)
    return keys


def _declaring_schemas(old_key: str, specs: list[tuple[str, dict, dict]]) -> list[tuple[str, str]]:
    """Return ``(filename, schema_name)`` for every schema declaring *old_key*."""
    return [
        (name, schema_name)
        for name, original, _ in specs
        for schema_name, schema in _schemas(original).items()
        if old_key in schema.get("properties", {})
    ]


def _corrected_schemas(new_key: str, specs: list[tuple[str, dict, dict]]) -> list[tuple[str, str]]:
    """Return ``(filename, schema_name)`` for every schema already using *new_key*.

    The published artifact is pipeline *output*, so a correction the pipeline
    already applies to the wire (a verified ``fix_spec``) is absent from it as
    the typo and present as the fix. That is success, not dead config.
    """
    return [
        (name, schema_name)
        for name, original, _ in specs
        for schema_name, schema in _schemas(original).items()
        if new_key in schema.get("properties", {})
    ]


def _annotated_objects(obj: Any, path: str = "") -> list[tuple[str, dict]]:
    """Return ``(dotted path, object)`` for every object carrying the annotation.

    Walks the whole document, not just ``components.schemas``: a guard that
    only looked where the transform writes today would stop holding the moment
    it writes somewhere else.
    """
    found: list[tuple[str, dict]] = []
    if isinstance(obj, dict):
        if WIRE_NAME_EXTENSION in obj:
            found.append((path, obj))
        for key, value in obj.items():
            found.extend(_annotated_objects(value, f"{path}.{key}" if path else key))
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            found.extend(_annotated_objects(item, f"{path}.{index}"))
    return found


def _ref_values(obj: Any, path: str = "") -> list[tuple[str, str]]:
    """Return ``(dotted path, target)`` for every ``$ref`` reachable in *obj*."""
    found: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else key
            if key == "$ref" and isinstance(value, str):
                found.append((child, value))
            else:
                found.extend(_ref_values(value, child))
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            found.extend(_ref_values(item, f"{path}.{index}"))
    return found


def _string_values(obj: Any) -> list[str]:
    """Collect every string value reachable in *obj*."""
    if isinstance(obj, dict):
        return [s for value in obj.values() for s in _string_values(value)]
    if isinstance(obj, list):
        return [s for item in obj for s in _string_values(item)]
    if isinstance(obj, str):
        return [obj]
    return []


class TestCorrectionsConfig:
    def test_config_declares_corrections(self):
        assert CORRECTIONS, f"no corrections loaded from {CONFIG_PATH}"

    @pytest.mark.parametrize("correction", CORRECTIONS, ids=CORRECTION_IDS)
    def test_every_entry_is_fully_specified(self, correction):
        for field in ("schema", "old_key", "new_key", "verified", "api_status"):
            assert field in correction, f"{correction.get('old_key')} lacks {field}"
        assert correction["api_status"] in {
            FIX_SPEC_STATUS,
            "upstream_typo",
            "upstream_typo_permanent",
            "unverifiable",
        }

    @pytest.mark.parametrize("correction", CORRECTIONS, ids=CORRECTION_IDS)
    def test_unverified_entries_are_unverifiable_not_unchecked(self, correction):
        if not correction["verified"]:
            assert correction["api_status"] == "unverifiable"
            assert correction.get("notes"), "an unverifiable entry must say why"


class TestReleasedSpecsAreCorrected:
    @pytest.mark.parametrize("correction", CORRECTIONS, ids=CORRECTION_IDS)
    def test_correction_matches_a_released_spec(self, correction, transformed_specs):
        """A tracked correction the artifact knows nothing about is dead config."""
        old_key, new_key = correction["old_key"], correction["new_key"]
        assert _declaring_schemas(old_key, transformed_specs) or _corrected_schemas(
            new_key, transformed_specs
        ), f"neither {old_key} nor {new_key} is declared by any released spec"

    @pytest.mark.parametrize("correction", CORRECTIONS, ids=CORRECTION_IDS)
    def test_presented_name_is_corrected(self, correction, transformed_specs):
        old_key, new_key = correction["old_key"], correction["new_key"]
        declaring = _declaring_schemas(old_key, transformed_specs)
        if not declaring:
            assert _corrected_schemas(new_key, transformed_specs), (
                f"{old_key} vanished from the artifact without {new_key} taking its place"
            )
            return
        for name, schema_name in declaring:
            after = next(
                _schemas(t)[schema_name]["properties"]
                for filename, _o, t in transformed_specs
                if filename == name
            )
            assert old_key not in after, f"{name}/{schema_name} kept {old_key}"
            assert new_key in after, f"{name}/{schema_name} lacks {new_key}"

    @pytest.mark.parametrize("correction", CORRECTIONS, ids=CORRECTION_IDS)
    def test_wire_key_is_preserved(self, correction, transformed_specs):
        old_key, new_key = correction["old_key"], correction["new_key"]
        declaring = _declaring_schemas(old_key, transformed_specs)
        if not declaring:
            # The typo is gone from the published artifact, so the wire key
            # already moved. Only a verified ``fix_spec`` may do that; for any
            # other status this is the regression that broke the field on the
            # wire downstream (terraform-provider-xcsh#1257).
            assert _changes_the_wire(correction), (
                f"{old_key} is absent from the published specs but its status "
                f"is {correction['api_status']!r} -- the wire key moved off a "
                "key the platform still requires"
            )
            return
        for name, schema_name in declaring:
            prop = next(
                _schemas(t)[schema_name]["properties"][new_key]
                for filename, _o, t in transformed_specs
                if filename == name
            )
            if _changes_the_wire(correction):
                assert WIRE_NAME_EXTENSION not in prop, (
                    f"{name}/{schema_name}: a verified fix_spec rename must not pin a wire name"
                )
            else:
                assert prop[WIRE_NAME_EXTENSION] == old_key, (
                    f"{name}/{schema_name}: wire key moved off {old_key}"
                )

    @pytest.mark.parametrize("correction", CORRECTIONS, ids=CORRECTION_IDS)
    def test_no_string_reference_to_the_typo_survives(self, correction, transformed_specs):
        """`required` and `x-ves-oneof-field-*` name properties by string."""
        old_key = correction["old_key"]
        for name, _original, transformed in transformed_specs:
            for schema_name, schema in _schemas(transformed).items():
                assert old_key not in schema.get("required", []), (
                    f"{name}/{schema_name}: required still names {old_key}"
                )
                for key, value in schema.items():
                    if not key.startswith(ONEOF_FIELD_PREFIX):
                        continue
                    assert old_key not in _string_values(value), (
                        f"{name}/{schema_name}/{key} still names {old_key}"
                    )
                    if isinstance(value, str):
                        assert f'"{old_key}"' not in value, (
                            f"{name}/{schema_name}/{key} still names {old_key}"
                        )


class TestAnnotationIsLegalOAS3:
    """The annotation must never make the artifact illegal OpenAPI 3.0.

    A ``$ref`` *replaces* the object holding it, so OAS3 forbids siblings:
    every resolver discards them and Spectral reports ``no-$ref-siblings`` as
    an **error**, which the release gate caps at zero.  Annotating a property
    that is a ``$ref`` therefore has to wrap it in ``allOf`` rather than sit
    beside it.  Without this guard the first ``$ref``-shaped correction blocks
    every release for every consumer -- which is exactly what happened (#698).

    Data-driven over the real config and the real specs, so a correction added
    later is covered the day it lands.
    """

    def test_no_wire_annotation_is_a_ref_sibling(self, transformed_specs):
        """The guard: not one annotation anywhere may share an object with ``$ref``."""
        offenders = [
            f"{name}: {path or '<root>'}"
            for name, _original, transformed in transformed_specs
            for path, obj in _annotated_objects(transformed)
            if "$ref" in obj
        ]
        assert not offenders, (
            f"{len(offenders)} {WIRE_NAME_EXTENSION} annotation(s) sit next to a $ref, "
            "which Spectral rejects as no-$ref-siblings and the release gate "
            f"caps at zero errors: {offenders}"
        )

    def test_the_guard_is_not_vacuous(self, transformed_specs):
        """A guard that never sees an annotation cannot catch a bad one."""
        annotated = [
            path
            for _n, _o, transformed in transformed_specs
            for path, _ in _annotated_objects(transformed)
        ]
        assert annotated, "the transform emitted no annotation at all"

    def test_a_wrapped_property_still_references_the_same_schema(self, transformed_specs):
        """Wrapping may move the ``$ref``; it may never change its target."""
        renames = {c["old_key"]: c["new_key"] for c in CORRECTIONS}
        for name, original, transformed in transformed_specs:
            after = _schemas(transformed)
            for schema_name, schema in _schemas(original).items():
                for prop_name, prop in schema.get("properties", {}).items():
                    if not isinstance(prop, dict) or "$ref" not in prop:
                        continue
                    corrected = renames.get(prop_name, prop_name)
                    now = after[schema_name]["properties"][corrected]
                    refs = sorted(ref for _p, ref in _ref_values(now))
                    assert refs == [prop["$ref"]], (
                        f"{name}/{schema_name}/{prop_name}: $ref target changed"
                    )


class TestNothingElseMoves:
    def test_no_wire_key_changes_anywhere(self, transformed_specs):
        """Every schema must put exactly the same JSON keys on the wire.

        The only permitted difference is a verified ``fix_spec`` correction,
        where the live API uses the corrected key and the pre-change transform
        already renamed it outright.
        """
        wire_renames = {c["old_key"]: c["new_key"] for c in CORRECTIONS if _changes_the_wire(c)}
        for name, original, transformed in transformed_specs:
            after = _schemas(transformed)
            for schema_name, schema in _schemas(original).items():
                expected = {wire_renames.get(key, key) for key in schema.get("properties", {})}
                assert _wire_keys(after[schema_name]) == expected, (
                    f"{name}/{schema_name}: wire keys changed"
                )

    def test_paths_operation_ids_and_refs_are_untouched(self, transformed_specs):
        for name, original, transformed in transformed_specs:
            assert transformed.get("paths") == original.get("paths"), name

    def test_enum_values_are_untouched(self, transformed_specs):
        for name, original, transformed in transformed_specs:
            after = _schemas(transformed)
            for schema_name, schema in _schemas(original).items():
                for prop_name, prop in schema.get("properties", {}).items():
                    if not isinstance(prop, dict) or "enum" not in prop:
                        continue
                    corrected = next(
                        (c["new_key"] for c in CORRECTIONS if c["old_key"] == prop_name),
                        prop_name,
                    )
                    assert after[schema_name]["properties"][corrected]["enum"] == prop["enum"], (
                        f"{name}/{schema_name}/{prop_name}"
                    )

    def test_schema_names_are_untouched(self, transformed_specs):
        for name, original, transformed in transformed_specs:
            assert set(_schemas(transformed)) == set(_schemas(original)), name

    def test_transform_is_idempotent(self, transformed_specs):
        config = TransformConfig(metadata={"property_name_corrections": CORRECTIONS})
        for name, _original, transformed in transformed_specs:
            twice = fix_property_names(copy.deepcopy(transformed), config, name)
            assert twice == transformed, f"{name} is not idempotent"
