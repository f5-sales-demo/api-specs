"""Correction and sanitization transforms for the OAS3 pipeline."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from ..example_identifier_safety import sanitize_identifier_examples
from ..pii_sanitizer import sanitize_pii_strings
from ..utils.text_replacements import (
    DEFAULT_EXAMPLE_PLACEHOLDER_FIELDS,
    DEFAULT_SPELLING_TEXT_FIELDS,
    build_replacement_patterns,
    replace_text_fields_recursive,
)

if TYPE_CHECKING:
    from ..transform import TransformConfig

SCHEMA_REF_PREFIX = "#/components/schemas/"
STUB_REMEDY = "stub"
WIRE_NAME_EXTENSION = "x-f5xc-wire-name"
FIX_SPEC_STATUS = "fix_spec"
ONEOF_FIELD_PREFIX = "x-ves-oneof-field-"
_MIRRORED_LABEL_KEYS = ("title", "x-displayname")


def _fix_examples_recursive(obj: Any) -> None:
    """Remove ``default``/``example`` keys whose value is not in a sibling ``enum``."""
    if isinstance(obj, dict):
        if "enum" in obj:
            enum_values = obj["enum"]
            for key in ("default", "example"):
                if key in obj and obj[key] not in enum_values:
                    del obj[key]
        for value in obj.values():
            _fix_examples_recursive(value)
    elif isinstance(obj, list):
        for item in obj:
            _fix_examples_recursive(item)


def _rewrite_refs(obj: Any, old_ref: str, new_ref: str) -> int:
    """Recursively rewrite ``$ref`` values matching *old_ref*.  Returns count."""
    count = 0
    if isinstance(obj, dict):
        if obj.get("$ref") == old_ref:
            obj["$ref"] = new_ref
            count += 1
        for value in obj.values():
            count += _rewrite_refs(value, old_ref, new_ref)
    elif isinstance(obj, list):
        for item in obj:
            count += _rewrite_refs(item, old_ref, new_ref)
    return count


def _preserves_wire_key(rule: dict) -> bool:
    """Return ``True`` when F5's API still requires the misspelled key.

    Only a correction that has been verified against the live API *and* found
    to use the corrected key (``api_status: fix_spec``) may change the wire.
    Everything else -- an upstream platform typo, or a correction nobody has
    been able to probe -- keeps the original key so requests stay byte
    identical to what the platform accepts today.
    """
    return not (rule.get("verified", False) and rule.get("api_status") == FIX_SPEC_STATUS)


def _annotate_wire_name(prop: dict, wire_name: str) -> None:
    """Record *wire_name* on *prop* without ever giving a ``$ref`` a sibling.

    A ``$ref`` *replaces* the object holding it, so OpenAPI 3.0 forbids keys
    beside one: resolvers discard them and Spectral reports
    ``no-$ref-siblings``, an error the release gate caps at zero.  A property
    that is a ``$ref`` is therefore moved inside ``allOf`` first -- the OAS3
    way to carry keywords alongside a reference -- and the annotation becomes
    a sibling of the wrapper instead.

    The wrap is applied only when a ``$ref`` is present.  A plain object
    property (the common case) is annotated in place, because wrapping one
    that needs no wrapper would rewrite the shape of the whole artifact for
    nothing.  A property that already composes with ``allOf`` gains the
    annotation beside that ``allOf`` rather than a second nested one.
    """
    if "$ref" in prop:
        wrapped = {"$ref": prop.pop("$ref")}
        composed = prop.get("allOf")
        prop["allOf"] = [wrapped, *composed] if isinstance(composed, list) else [wrapped]

    prop[WIRE_NAME_EXTENSION] = wire_name


def _rename_key_in_place(mapping: dict, old_key: str, new_key: str) -> dict:
    """Return a copy of *mapping* with *old_key* renamed, keeping its position."""
    return {(new_key if key == old_key else key): value for key, value in mapping.items()}


def _rename_schema_property(
    schema_def: dict,
    old_key: str,
    new_key: str,
    wire_name: str | None,
) -> bool:
    """Rename one misspelled property everywhere *schema_def* names it.

    Covers the ``properties`` key itself, any mention in ``required``, and any
    ``x-ves-oneof-field-*`` group that lists the property by name.  When
    *wire_name* is given it is recorded on the renamed property by
    :func:`_annotate_wire_name`, so downstream consumers can present the
    corrected name while marshalling the original and the result stays legal
    OpenAPI 3.0.

    Returns ``True`` when the schema declared *old_key* and was rewritten.  A
    schema that already declares *new_key* is left alone: the two keys are
    distinct fields and renaming would silently destroy one of them.
    """
    props = schema_def.get("properties")
    if not isinstance(props, dict) or old_key not in props or new_key in props:
        return False

    schema_def["properties"] = _rename_key_in_place(props, old_key, new_key)

    prop = schema_def["properties"][new_key]
    if isinstance(prop, dict):
        for label_key in _MIRRORED_LABEL_KEYS:
            if prop.get(label_key) == old_key:
                prop[label_key] = new_key
        if wire_name is not None:
            _annotate_wire_name(prop, wire_name)

    required = schema_def.get("required")
    if isinstance(required, list):
        schema_def["required"] = [new_key if entry == old_key else entry for entry in required]

    for ext_key, ext_value in list(schema_def.items()):
        if not ext_key.startswith(ONEOF_FIELD_PREFIX):
            continue
        if isinstance(ext_value, str):
            schema_def[ext_key] = ext_value.replace(f'"{old_key}"', f'"{new_key}"')
        elif isinstance(ext_value, list):
            schema_def[ext_key] = [new_key if entry == old_key else entry for entry in ext_value]

    return True


def _collect_refs(obj: Any) -> set[str]:
    """Recursively collect all ``$ref`` strings."""
    refs: set[str] = set()
    if isinstance(obj, dict):
        if "$ref" in obj and isinstance(obj["$ref"], str):
            refs.add(obj["$ref"])
        for value in obj.values():
            refs.update(_collect_refs(value))
    elif isinstance(obj, list):
        for item in obj:
            refs.update(_collect_refs(item))
    return refs


def find_dangling_refs(spec: dict) -> list[tuple[str, str]]:
    """Return ``(path, schema name)`` for every local ``$ref`` with no definition.

    Only ``#/components/schemas/`` references are considered: they are the only
    ones this pipeline can resolve, and they are the ones Spectral rejects as
    ``invalid-ref`` errors. *path* is the dotted location of the ``$ref`` key,
    matching the ``property_name`` format the Spectral report uses, so a
    finding can be quoted straight into a failure message.
    """
    schemas = (spec.get("components") or {}).get("schemas") or {}
    dangling: list[tuple[str, str]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}" if path else key
                if key != "$ref" or not isinstance(value, str):
                    walk(value, child)
                elif value.startswith(SCHEMA_REF_PREFIX):
                    name = value[len(SCHEMA_REF_PREFIX) :]
                    if name not in schemas:
                        dangling.append((child, name))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}.{index}")

    walk(spec, "")
    return dangling


def fix_invalid_examples(
    spec: dict,
    _config: TransformConfig,
    _filename: str,
) -> dict:
    """Remove ``default``/``example`` values that violate ``enum`` constraints."""
    _fix_examples_recursive(spec)
    return spec


def rename_colliding_schemas(
    spec: dict,
    config: TransformConfig,
    filename: str,
) -> dict:
    """Rename schemas that collide across domain files."""
    renames = config.reconciliation_config.get("schema_renames", [])
    schemas = spec.get("components", {}).get("schemas", {})

    for rule in renames:
        old_name = rule["old_name"]
        new_name = rule["new_name"]
        pattern = rule.get("file_pattern", "")

        if pattern and pattern not in filename:
            continue
        if old_name not in schemas:
            continue

        schemas[new_name] = schemas.pop(old_name)
        old_ref = f"#/components/schemas/{old_name}"
        new_ref = f"#/components/schemas/{new_name}"
        _rewrite_refs(spec, old_ref, new_ref)

    return spec


def fix_dangling_refs(
    spec: dict,
    config: TransformConfig,
    filename: str,
) -> dict:
    """Repair ``$ref`` targets that upstream references but never defines.

    A dangling reference is an ``invalid-ref`` error to Spectral, and the
    release gate allows zero errors, so one of them stops the publish for
    every consumer. Each repair is declared in
    ``config/dangling_ref_corrections.yaml`` -- see that file for the rules a
    correction must satisfy.

    Runs before :func:`remove_unused_schemas` so an injected stub takes part
    in the reachability walk like any other schema.
    """
    corrections = config.metadata.get("dangling_ref_corrections", [])
    if not corrections:
        return spec

    for rule in corrections:
        schema_name = rule.get("schema", "<unnamed>")
        remedy = rule.get("remedy")
        if remedy != STUB_REMEDY:
            msg = (
                f"{schema_name}: unsupported dangling-ref remedy {remedy!r}; "
                f"only {STUB_REMEDY!r} is implemented"
            )
            raise ValueError(msg)
        if not rule.get("definition"):
            msg = f"{schema_name}: {STUB_REMEDY} correction without a definition"
            raise ValueError(msg)

    # Names only: a stub resolves every reference to it at once.
    dangling = {name for _, name in find_dangling_refs(spec)}
    if not dangling:
        return spec

    for rule in corrections:
        schema_name = rule["schema"]
        file_pattern = rule.get("file_pattern")
        if file_pattern and file_pattern not in filename:
            continue
        if schema_name not in dangling:
            continue

        schemas = spec.setdefault("components", {}).setdefault("schemas", {})
        schemas[schema_name] = copy.deepcopy(rule["definition"])

    return spec


def fix_property_names(
    spec: dict,
    config: TransformConfig,
    _filename: str,
) -> dict:
    """Correct misspelled JSON property names in component schemas.

    Every tracked correction fixes the *presented* name -- the Terraform
    attribute, MCP field and documentation label that consumers see.  Where
    F5's platform still requires the misspelling on the wire, the renamed
    property carries ``x-f5xc-wire-name`` holding the original key verbatim,
    so no consumer has to hard-code the typo and no request changes shape.

    The ``schema`` recorded on each correction names the schema the typo was
    probed against; the rename is applied to every schema that declares the
    key, because F5 repeats the same misspelled field across sibling schemas
    and files.

    This supersedes the ``never_apply`` flag.  ``never_apply`` protected the
    wire key by refusing to rename at all, which also meant the typo stayed
    in every generated consumer.  The wire key is now protected structurally
    instead: ``api_status`` decides whether the rename reaches the wire, and
    anything short of a verified ``fix_spec`` keeps the original key in
    ``x-f5xc-wire-name``.
    """
    corrections = config.metadata.get("property_name_corrections", [])
    if not corrections:
        return spec

    schemas = spec.get("components", {}).get("schemas", {})
    for rule in corrections:
        old_key = rule["old_key"]
        new_key = rule["new_key"]
        wire_name = old_key if _preserves_wire_key(rule) else None

        for schema_def in schemas.values():
            if isinstance(schema_def, dict):
                _rename_schema_property(schema_def, old_key, new_key, wire_name)

    return spec


def fix_oneof_group_names(
    spec: dict,
    config: TransformConfig,
    _filename: str,
) -> dict:
    """Correct misspelled ``x-ves-oneof-field-*`` group names (#690).

    :func:`fix_property_names` corrects the member names in the extension's *value*; the group
    name lives in its *key*, which that transform leaves alone. Runs after it, so key and value
    agree once this returns. See ``config/oneof_group_corrections.yaml``.

    Cannot move the wire: ``x-ves-oneof-*`` is schema metadata, never a field the API accepts,
    so there is no wire key to preserve. ``properties`` is untouched, which
    ``test_oneof_group_corrections.py::test_properties_are_never_touched`` enforces.
    """
    corrections = config.metadata.get("oneof_group_corrections", [])
    if not corrections:
        return spec

    renames = {
        f"{ONEOF_FIELD_PREFIX}{rule['old_group']}": f"{ONEOF_FIELD_PREFIX}{rule['new_group']}"
        for rule in corrections
    }

    schemas = spec.get("components", {}).get("schemas", {})
    for schema_def in schemas.values():
        if not isinstance(schema_def, dict):
            continue
        for old_key, new_key in renames.items():
            if old_key in schema_def:
                # Rebuild rather than pop-and-set so the corrected key keeps the original's
                # position; a moved key would show up as spurious churn in the artifact diff.
                rebuilt = {(new_key if k == old_key else k): v for k, v in schema_def.items()}
                schema_def.clear()
                schema_def.update(rebuilt)

    return spec


def fix_spelling(
    spec: dict,
    config: TransformConfig,
    _filename: str,
) -> dict:
    """Fix known spelling errors in the configured prose fields."""
    corrections = config.metadata.get("spelling_corrections", {})
    if not corrections:
        return spec
    text_fields = config.metadata.get("spelling_text_fields", DEFAULT_SPELLING_TEXT_FIELDS)
    patterns = build_replacement_patterns(corrections)
    replace_text_fields_recursive(spec, patterns, text_fields)
    return spec


def sanitize_example_placeholders(
    spec: dict,
    config: TransformConfig,
    _filename: str,
) -> dict:
    """Replace configured unsafe placeholders in documentation-bearing fields."""
    corrections = config.metadata.get("example_placeholder_corrections", {})
    if not corrections:
        return spec
    text_fields = config.metadata.get(
        "example_placeholder_fields", DEFAULT_EXAMPLE_PLACEHOLDER_FIELDS
    )
    patterns = build_replacement_patterns(corrections)
    replace_text_fields_recursive(spec, patterns, text_fields)
    return spec


def sanitize_example_identifiers(
    spec: dict,
    _config: TransformConfig,
    _filename: str,
) -> dict:
    """Replace realistic UUID and private-address examples with synthetic values."""
    return sanitize_identifier_examples(spec)


def sanitize_pii_placeholders(
    spec: dict,
    _config: TransformConfig,
    _filename: str,
) -> dict:
    """Remove contact, person, and customer literals from every emitted string."""

    return sanitize_pii_strings(spec)
