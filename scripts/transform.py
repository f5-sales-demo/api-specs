"""OAS3 spec transform pipeline -- clean upstream specs before validation."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from rich.console import Console

from .check_pii import EMAIL_RE, IDENTITY_FIELD_RE, PERSON_FIELD_RE, placeholder_value, safe_email
from .example_identifier_safety import sanitize_identifier_examples
from .utils.nullable_response import apply_nullable_response_corrections
from .utils.spec_loader import save_spec_to_file
from .utils.text_replacements import (
    DEFAULT_EXAMPLE_PLACEHOLDER_FIELDS,
    DEFAULT_SPELLING_TEXT_FIELDS,
    build_replacement_patterns,
    replace_text_fields_recursive,
)

if TYPE_CHECKING:
    from collections.abc import Callable

console = Console()

# All standard HTTP methods recognised in OAS3 path items.
HTTP_METHODS: frozenset[str] = frozenset(
    {"get", "post", "put", "delete", "patch", "options", "head", "trace"}
)

# Index of the path segment used to derive an operation tag.
# For ``/api/config/namespaces/...`` the tag is ``config`` (index 1).
_TAG_SEGMENT_INDEX = 1

# Prefix of a local component-schema reference.
SCHEMA_REF_PREFIX = "#/components/schemas/"

# The only remedy ``fix_dangling_refs`` implements. Declared in
# ``dangling_ref_corrections.yaml`` so a correction states its intent and an
# unimplemented one fails loudly instead of silently doing nothing.
STUB_REMEDY = "stub"

# Extension that records the JSON key F5's live API accepts on the wire.
# Emitted whenever a misspelled property is renamed for presentation but the
# platform still requires the original key in requests and responses.
WIRE_NAME_EXTENSION = "x-f5xc-wire-name"

# ``api_status`` value meaning the live API itself uses the corrected key, so
# renaming changes the wire too and no wire-name annotation is needed.
FIX_SPEC_STATUS = "fix_spec"

# Prefix of the schema-level extensions whose value enumerates the member
# property names of a ``oneof`` group.  F5 emits the list as a JSON-encoded
# string (``"[\"a\",\"b\"]"``), so a renamed member has to be rewritten there
# too or the group stops naming a property that exists.
ONEOF_FIELD_PREFIX = "x-ves-oneof-field-"

# Presentation strings that mirror the property key verbatim in some F5
# schemas; they follow the rename so the typo does not resurface in docs.
_MIRRORED_LABEL_KEYS = ("title", "x-displayname")

# ---------------------------------------------------------------------------
# Transform registry
# ---------------------------------------------------------------------------

TRANSFORM_REGISTRY: list[tuple[str, Callable[..., dict]]] = []


def register_transform(name: str) -> Callable:
    """Decorator that appends a transform function to the global registry."""

    def wrapper(fn: Callable[..., dict]) -> Callable[..., dict]:
        TRANSFORM_REGISTRY.append((name, fn))
        return fn

    return wrapper


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TransformConfig:
    """Configuration for the transform pipeline."""

    input_dir: str = "specs/original"
    output_dir: str = "release/specs"
    transforms: dict[str, bool] = field(default_factory=dict)
    spectral_config: dict[str, Any] = field(default_factory=dict)
    reconciliation_config: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TransformResult:
    """Result of transforming a single spec file."""

    filename: str
    spec: dict
    changes: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_scripts_recursive(obj: Any) -> None:
    """Walk *obj* in-place and strip ``<script>`` tags from ``description`` fields."""
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
            _strip_scripts_recursive(value)
    elif isinstance(obj, list):
        for item in obj:
            _strip_scripts_recursive(item)


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


# ---------------------------------------------------------------------------
# Transform functions (registration order matters)
# ---------------------------------------------------------------------------


@register_transform("inject_info_version")
def inject_info_version(
    spec: dict,
    config: TransformConfig,
    _filename: str,
) -> dict:
    """Set ``info.version`` from pipeline metadata."""
    version = config.metadata.get("spec_date") or config.metadata.get("download_date", "")
    spec.setdefault("info", {})["version"] = version
    return spec


@register_transform("inject_contact")
def inject_contact(
    spec: dict,
    config: TransformConfig,
    _filename: str,
) -> dict:
    """Add ``info.contact`` from spectral config."""
    contact = config.spectral_config.get("contact")
    if contact is not None:
        spec.setdefault("info", {})["contact"] = copy.deepcopy(contact)
    return spec


@register_transform("inject_servers")
def inject_servers(
    spec: dict,
    config: TransformConfig,
    _filename: str,
) -> dict:
    """Add ``servers`` from spectral config."""
    servers = config.spectral_config.get("servers")
    if servers is not None:
        spec["servers"] = copy.deepcopy(servers)
    return spec


@register_transform("inject_security_schemes")
def inject_security_schemes(
    spec: dict,
    config: TransformConfig,
    _filename: str,
) -> dict:
    """Add ``components.securitySchemes.apiKeyAuth`` and global ``security``."""
    security_config = config.spectral_config.get("security_scheme")
    if security_config is None:
        return spec

    scheme_name = "apiKeyAuth"
    spec.setdefault("components", {}).setdefault("securitySchemes", {})[scheme_name] = {
        "type": security_config.get("type", "apiKey"),
        "in": security_config.get("in", "header"),
        "name": security_config.get("name", "Authorization"),
        "description": security_config.get(
            "description", "F5 XC API Token (format: APIToken <token>)"
        ),
    }
    spec.setdefault("security", [{"apiKeyAuth": []}])
    return spec


@register_transform("inject_operation_tags")
def inject_operation_tags(
    spec: dict,
    _config: TransformConfig,
    _filename: str,
) -> dict:
    """Derive and inject tags from URL path segments for every operation."""
    for path_key, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue

        segments = [s for s in path_key.split("/") if s and not s.startswith("{")]
        tag = "default"
        if len(segments) > _TAG_SEGMENT_INDEX:
            tag = segments[_TAG_SEGMENT_INDEX]
        elif segments:
            tag = segments[0]

        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            operation["tags"] = [tag]

        existing_tags = spec.setdefault("tags", [])
        if not any(t.get("name") == tag for t in existing_tags):
            existing_tags.append({"name": tag})

    return spec


@register_transform("deduplicate_operation_ids")
def deduplicate_operation_ids(
    spec: dict,
    _config: TransformConfig,
    _filename: str,
) -> dict:
    """Append ``_{method}`` suffix to every occurrence of duplicate operationIds."""
    # Pass 1: collect all operationIds and their locations.
    id_locations: dict[str, list[tuple[str, str]]] = {}
    for path_key, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            op_id = operation.get("operationId")
            if op_id is None:
                continue
            id_locations.setdefault(op_id, []).append((path_key, method))

    # Pass 2: rename duplicates with method suffix, adding index for same-method collisions.
    for op_id, locations in id_locations.items():
        if len(locations) <= 1:
            continue
        method_counts: dict[str, int] = {}
        for path_key, method in locations:
            count = method_counts.get(method, 0)
            operation = spec["paths"][path_key][method]
            if count == 0:
                operation["operationId"] = f"{op_id}_{method}"
            else:
                operation["operationId"] = f"{op_id}_{method}_{count}"
            method_counts[method] = count + 1

    return spec


@register_transform("strip_script_tags")
def strip_script_tags(
    spec: dict,
    _config: TransformConfig,
    _filename: str,
) -> dict:
    """Strip ``<script>`` tags from all ``description`` fields recursively."""
    _strip_scripts_recursive(spec)
    return spec


@register_transform("fix_invalid_examples")
def fix_invalid_examples(
    spec: dict,
    _config: TransformConfig,
    _filename: str,
) -> dict:
    """Remove ``default``/``example`` values that violate ``enum`` constraints."""
    _fix_examples_recursive(spec)
    return spec


@register_transform("rename_colliding_schemas")
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


@register_transform("remove_deprecated_paths")
def remove_deprecated_paths(
    spec: dict,
    config: TransformConfig,
    _filename: str,
) -> dict:
    """Remove paths listed in ``reconciliation_config.deprecated_path_removals``."""
    removals = config.reconciliation_config.get("deprecated_path_removals", [])
    for rule in removals:
        target = rule["path"]
        if target in spec.get("paths", {}):
            del spec["paths"][target]
    return spec


@register_transform("mark_deprecated_operations")
def mark_deprecated_operations(
    spec: dict,
    _config: TransformConfig,
    _filename: str,
) -> dict:
    """Set ``deprecated: true`` on operations whose description contains DEPRECATED."""
    for path_item in spec.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            desc = op.get("description", "")
            if "DEPRECATED" in desc.upper() and not op.get("deprecated"):
                op["deprecated"] = True
    return spec


@register_transform("fix_dangling_refs")
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


@register_transform("mark_nullable_response_fields")
def mark_nullable_response_fields(
    spec: dict,
    config: TransformConfig,
    filename: str,
) -> dict:
    """Apply fail-closed, measured response nullability corrections."""
    return apply_nullable_response_corrections(
        spec,
        config.metadata.get("nullable_response_corrections", []),
        filename,
    )


@register_transform("remove_unused_schemas")
def remove_unused_schemas(
    spec: dict,
    _config: TransformConfig,
    _filename: str,
) -> dict:
    """Remove component schemas that are not reachable from paths or other used schemas."""
    schemas = spec.get("components", {}).get("schemas")
    if not schemas:
        return spec

    # Collect refs from everything *except* the schemas section.
    external_refs: set[str] = set()
    for top_key, top_value in spec.items():
        if top_key == "components":
            # Scan components sections other than schemas.
            if isinstance(top_value, dict):
                for comp_key, comp_value in top_value.items():
                    if comp_key != "schemas":
                        external_refs.update(_collect_refs(comp_value))
        else:
            external_refs.update(_collect_refs(top_value))

    # Seed: schemas referenced externally.
    prefix = SCHEMA_REF_PREFIX
    reachable: set[str] = set()
    frontier = [
        ref[len(prefix) :]
        for ref in external_refs
        if ref.startswith(prefix) and ref[len(prefix) :] in schemas
    ]

    # Walk schema graph.
    while frontier:
        name = frontier.pop()
        if name in reachable:
            continue
        reachable.add(name)
        schema_obj = schemas.get(name)
        if schema_obj is None:
            continue
        for ref in _collect_refs(schema_obj):
            if ref.startswith(prefix):
                child = ref[len(prefix) :]
                if child in schemas and child not in reachable:
                    frontier.append(child)

    # Remove unreachable schemas.
    to_remove = set(schemas.keys()) - reachable
    for name in to_remove:
        del schemas[name]

    return spec


@register_transform("fix_property_names")
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


@register_transform("fix_oneof_group_names")
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


@register_transform("fix_spelling")
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


@register_transform("sanitize_example_placeholders")
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


@register_transform("sanitize_example_identifiers")
def sanitize_example_identifiers(
    spec: dict,
    _config: TransformConfig,
    _filename: str,
) -> dict:
    """Replace realistic UUID and private-address examples with synthetic values."""
    return sanitize_identifier_examples(spec)


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


@register_transform("sanitize_pii_placeholders")
def sanitize_pii_placeholders(
    spec: dict,
    _config: TransformConfig,
    _filename: str,
) -> dict:
    """Remove contact, person, and customer literals from every emitted string."""

    def sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: sanitize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return _sanitize_pii_text(value) if isinstance(value, str) else value

    return sanitize(spec)


@register_transform("inject_operation_descriptions")
def inject_operation_descriptions(
    spec: dict,
    _config: TransformConfig,
    _filename: str,
) -> dict:
    """Generate stub descriptions for operations that lack one."""
    for path_key, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            if operation.get("description"):
                continue

            # Derive action from operationId (last dot-segment).
            op_id = operation.get("operationId", "")
            action = op_id.rsplit(".", 1)[-1] if "." in op_id else op_id

            # Derive resource from last non-parameter path segment.
            segments = [s for s in path_key.split("/") if s and not s.startswith("{")]
            resource = segments[-1] if segments else "resource"

            operation["description"] = f"{action} {resource}."

    return spec


# ---------------------------------------------------------------------------
# Transformer class
# ---------------------------------------------------------------------------


class SpecTransformer:
    """Apply registered transforms to all spec files in a directory."""

    def __init__(self, config: TransformConfig) -> None:
        """Initialise the transformer with *config*."""
        self.config = config
        self.results: list[TransformResult] = []

    def transform_all(self) -> list[TransformResult]:
        """Load every spec from *input_dir* and run all enabled transforms."""
        input_path = Path(self.config.input_dir)
        self.results = []

        for spec_file in sorted(input_path.glob("*.json")):
            if spec_file.name.startswith("."):
                continue
            result = self._transform_file(spec_file)
            self.results.append(result)
            console.print(f"  [dim]{result.filename}: {len(result.changes)} changes[/dim]")

        return self.results

    def _transform_file(self, spec_path: Path) -> TransformResult:
        """Run every enabled transform on a single spec file."""
        with spec_path.open() as fh:
            spec = json.load(fh)

        changes: list[dict] = []
        for name, fn in TRANSFORM_REGISTRY:
            if not self.config.transforms.get(name, True):
                continue

            before = json.dumps(spec, sort_keys=True)
            spec = fn(spec, self.config, spec_path.name)
            after = json.dumps(spec, sort_keys=True)

            if before != after:
                changes.append({"transform": name})

        return TransformResult(
            filename=spec_path.name,
            spec=spec,
            changes=changes,
        )

    def save_results(self) -> dict[str, Path]:
        """Write transformed specs to *output_dir*."""
        output_path = Path(self.config.output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        saved: dict[str, Path] = {}
        for result in self.results:
            dest = output_path / result.filename
            save_spec_to_file(result.spec, dest, "json")
            saved[result.filename] = dest

        return saved


# ---------------------------------------------------------------------------
# Configuration loader
# ---------------------------------------------------------------------------


def load_spec_metadata(specs_dir: str | Path) -> dict[str, Any]:
    """Return the provenance metadata recorded alongside the specs in *specs_dir*.

    ``inject_info_version`` stamps ``info.version`` from ``spec_date``, so this must describe the
    drop the specs in *specs_dir* actually came from. Reading it from anywhere else -- most
    obviously from ``specs/original`` while transforming ``release/specs`` -- stamps an unrelated
    local download's date into every published spec.

    Returns an empty dict when the file is absent, which leaves ``info.version`` unset rather than
    guessing.
    """
    metadata_path = Path(specs_dir) / ".spec_metadata.json"
    if not metadata_path.exists():
        return {}
    with metadata_path.open() as fh:
        return json.load(fh)


def load_config(config_path: str | Path) -> TransformConfig:
    """Build a ``TransformConfig`` from *config_path* (``validation.yaml``)."""
    config_path = Path(config_path)
    if not config_path.exists():
        return TransformConfig()

    with config_path.open() as fh:
        raw = yaml.safe_load(fh) or {}

    download_cfg = raw.get("download", {})
    transform_cfg = raw.get("transform", {})
    spectral_cfg = raw.get("spectral", {})
    reconciliation_cfg = raw.get("reconciliation", {})

    input_dir = Path(download_cfg.get("output_dir", "specs/original"))
    output_dir = Path(transform_cfg.get("output_dir", "specs/transformed"))

    metadata: dict[str, Any] = load_spec_metadata(input_dir)

    spelling_path = config_path.parent / "spelling_corrections.yaml"
    if spelling_path.exists():
        with spelling_path.open() as fh:
            spelling_cfg = yaml.safe_load(fh) or {}
        metadata["spelling_corrections"] = spelling_cfg.get("corrections", {})
        metadata["spelling_text_fields"] = tuple(
            spelling_cfg.get("text_fields") or DEFAULT_SPELLING_TEXT_FIELDS
        )

    placeholders_path = config_path.parent / "example_placeholder_corrections.yaml"
    if placeholders_path.exists():
        with placeholders_path.open() as fh:
            placeholders_cfg = yaml.safe_load(fh) or {}
        metadata["example_placeholder_corrections"] = placeholders_cfg.get("corrections", {})
        metadata["example_placeholder_fields"] = tuple(
            placeholders_cfg.get("text_fields") or DEFAULT_EXAMPLE_PLACEHOLDER_FIELDS
        )

    # Each of these is a `corrections:` list in its own file, loaded the same way.
    for key in (
        "property_name_corrections",
        "oneof_group_corrections",
        "dangling_ref_corrections",
        "nullable_response_corrections",
    ):
        path = config_path.parent / f"{key}.yaml"
        if path.exists():
            with path.open() as fh:
                metadata[key] = (yaml.safe_load(fh) or {}).get("corrections", [])

    return TransformConfig(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        transforms=transform_cfg.get("transforms", {}),
        spectral_config=spectral_cfg,
        reconciliation_config=reconciliation_cfg,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """CLI entry point for ``python -m scripts.transform``."""
    parser = argparse.ArgumentParser(
        description="OAS3 transform pipeline for F5 XC API specs",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/validation.yaml"),
        help="Configuration file path",
    )
    parser.add_argument(
        "--input-dir",
        type=str,
        default=None,
        help="Directory containing downloaded specs",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Output directory for transformed specs",
    )

    args = parser.parse_args()

    config = load_config(args.config)
    if args.input_dir:
        config.input_dir = args.input_dir
        # Provenance follows the specs. Without this the run would keep the metadata
        # load_config read from the configured input dir (specs/original) and stamp that
        # download's date into specs taken from somewhere else -- e.g. regenerating
        # release/specs would rewrite info.version to whatever was last downloaded locally.
        config.metadata.update(load_spec_metadata(args.input_dir))
    if args.output_dir:
        config.output_dir = args.output_dir

    console.print("[bold blue]Running OAS3 Transform Pipeline[/bold blue]")

    transformer = SpecTransformer(config)
    results = transformer.transform_all()
    saved = transformer.save_results()

    total_changes = sum(len(r.changes) for r in results)
    console.print(
        f"\n[green]Transformed {len(saved)} specs ({total_changes} total changes)[/green]"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
