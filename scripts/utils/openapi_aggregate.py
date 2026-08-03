"""Build a deterministic, lossless aggregate from multiple OpenAPI documents."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote_to_bytes

from scripts.utils.strict_data import (
    canonical_json_bytes,
    strict_json_loads,
    strict_yaml_loads,
    validate_json_data,
)

HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def load_openapi_document(path: Path) -> dict[str, Any]:
    """Load one JSON or YAML OpenAPI input without lossy parser behaviour."""
    source = Path(path)
    try:
        content = source.read_text(encoding="utf-8")
        if source.suffix == ".json":
            document = strict_json_loads(content, source.name)
        elif source.suffix in {".yaml", ".yml"}:
            document = strict_yaml_loads(content, source.name)
        else:
            raise ValueError(f"unsupported OpenAPI input type: {source.name}")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"OpenAPI input cannot be read: {source.name}") from error
    if not isinstance(document, dict):
        raise ValueError(f"OpenAPI input is not an object: {source.name}")
    validate_json_data(document, source.name)
    return document


def _canonical(value: Any) -> bytes:
    return canonical_json_bytes(value)


def _pointer_decode(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")


def _pointer_decode_strict(value: str, ref: str) -> str:
    if re.fullmatch(r"(?:[^~]|~[01])*", value) is None:
        raise ValueError(f"aggregate contains malformed local reference: {ref}")
    return _pointer_decode(value)


def _pointer_encode(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _local_reference_tokens(ref: str) -> list[str]:
    if not ref.startswith("#/"):
        raise ValueError(f"aggregate contains non-local reference: {ref}")
    fragment = ref[1:]
    if re.search(r"%(?![0-9A-Fa-f]{2})", fragment):
        raise ValueError(f"aggregate contains malformed local reference: {ref}")
    try:
        pointer = unquote_to_bytes(fragment).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"aggregate contains malformed local reference: {ref}") from error
    if f"#{quote(pointer, safe='/~')}" != ref:
        raise ValueError(f"aggregate contains malformed local reference: {ref}")
    return [_pointer_decode_strict(token, ref) for token in pointer[1:].split("/")]


def _local_reference(tokens: list[str]) -> str:
    pointer = "/" + "/".join(_pointer_encode(token) for token in tokens)
    return f"#{quote(pointer, safe='/~')}"


def _rewrite_refs(value: Any, names: dict[str, dict[str, str]]) -> Any:
    if isinstance(value, list):
        return [_rewrite_refs(child, names) for child in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    rewritten: dict[str, Any] = {}
    for key, child in value.items():
        if key == "$ref" and isinstance(child, str):
            try:
                tokens = _local_reference_tokens(child)
            except ValueError:
                tokens = []
            if len(tokens) >= 3 and tokens[0] == "components":
                category = tokens[1]
                component = tokens[2]
                emitted = names.get(category, {}).get(component)
                if emitted is not None:
                    tokens[2] = emitted
                    child = _local_reference(tokens)
        rewritten[key] = _rewrite_refs(child, names)
    return rewritten


def _variant_name(name: str, sources: list[str], value: Any) -> str:
    identity = "\0".join(sources).encode() + b"\0" + _canonical(value)
    digest = hashlib.sha256(identity).hexdigest()[:16]
    source_slug = re.sub(r"[^A-Za-z0-9]+", "_", Path(sources[0]).stem).strip("_")
    if not source_slug:
        source_slug = "source"
    return f"{name}__{source_slug}__{digest}"


def _group_values(values: dict[str, Any]) -> list[tuple[list[str], Any]]:
    grouped: dict[bytes, list[str]] = defaultdict(list)
    exemplars: dict[bytes, Any] = {}
    for source, value in sorted(values.items()):
        identity = _canonical(value)
        grouped[identity].append(source)
        exemplars[identity] = value
    return [(sorted(grouped[identity]), exemplars[identity]) for identity in sorted(grouped)]


def _component_inventory(
    documents: list[tuple[str, dict[str, Any]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    inventory: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(dict))
    for source, document in documents:
        components = document.get("components", {})
        if not isinstance(components, dict):
            raise ValueError(f"{source} components must be an object")
        for category, entries in components.items():
            if not isinstance(entries, dict):
                raise ValueError(f"{source} components.{category} must be an object")
            for name, value in entries.items():
                inventory[category][name][source] = value
    return inventory


def _component_names(
    documents: list[tuple[str, dict[str, Any]]],
    inventory: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, dict[str, str]]]:
    names: dict[str, dict[str, dict[str, str]]] = {
        source: defaultdict(dict) for source, _ in documents
    }
    for category, entries in inventory.items():
        for name, values in entries.items():
            groups = _group_values(values)
            for sources, value in groups:
                emitted = name if len(groups) == 1 else _variant_name(name, sources, value)
                for source in sources:
                    names[source][category][name] = emitted

    # An otherwise identical wrapper can become source-specific when its local
    # reference is rewritten to a conflicting dependency. Iterate until those
    # transitive conflicts also receive distinct names.
    while _update_transitive_component_names(inventory, names):
        pass
    return names


def _update_transitive_component_names(
    inventory: dict[str, dict[str, dict[str, Any]]],
    names: dict[str, dict[str, dict[str, str]]],
) -> bool:
    changed = False
    for category, entries in inventory.items():
        for name, values in entries.items():
            rewritten = {
                source: _rewrite_refs(value, names[source]) for source, value in values.items()
            }
            groups = _group_values(rewritten)
            if len(groups) > 1:
                changed |= _assign_variant_names(names, category, name, groups, values)
    return changed


def _assign_variant_names(
    names: dict[str, dict[str, dict[str, str]]],
    category: str,
    name: str,
    groups: list[tuple[list[str], Any]],
    original_values: dict[str, Any],
) -> bool:
    changed = False
    for sources, _rewritten_value in groups:
        emitted = _variant_name(name, sources, original_values[sources[0]])
        for source in sources:
            if names[source][category][name] != emitted:
                names[source][category][name] = emitted
                changed = True
    return changed


def _preserve_inherited_root_semantics(
    path_item: Any,
    document: dict[str, Any],
    common_security: Any,
    common_servers: Any,
) -> Any:
    result = deepcopy(path_item)
    if not isinstance(result, dict):
        return result
    for method, operation in result.items():
        if method not in HTTP_METHODS or not isinstance(operation, dict):
            continue
        if common_security is None and "security" not in operation and "security" in document:
            operation["security"] = deepcopy(document["security"])
        if (
            common_servers is None
            and "servers" not in result
            and "servers" not in operation
            and "servers" in document
        ):
            operation["servers"] = deepcopy(document["servers"])
    return result


def _common_root_value(
    documents: list[tuple[str, dict[str, Any]]],
    field: str,
) -> Any | None:
    if not all(field in document for _, document in documents):
        return None
    values = [document[field] for _, document in documents]
    if len({_canonical(value) for value in values}) != 1:
        return None
    return deepcopy(values[0])


def _resolve_local_component_ref(ref: str, root: dict[str, Any]) -> None:
    tokens = _local_reference_tokens(ref)
    if len(tokens) < 3 or tokens[0] != "components" or not tokens[1] or not tokens[2]:
        raise ValueError(f"aggregate contains malformed local reference: {ref}")
    current: Any = root
    for token in tokens:
        if isinstance(current, dict) and token in current:
            current = current[token]
            continue
        if isinstance(current, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
            index = int(token)
            if index < len(current):
                current = current[index]
                continue
        raise ValueError(f"aggregate contains unresolved local reference: {ref}")


def _assert_component_refs_resolve(
    value: Any,
    root: dict[str, Any],
    location: str = "aggregate",
) -> None:
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_component_refs_resolve(child, root, f"{location}[{index}]")
        return
    if not isinstance(value, dict):
        return
    if "$ref" in value:
        ref = value["$ref"]
        if not isinstance(ref, str):
            raise ValueError(f"aggregate contains non-string reference at {location}.$ref")
        try:
            _resolve_local_component_ref(ref, root)
        except ValueError as error:
            raise ValueError(f"{error} at {location}.$ref") from error
    for key, child in value.items():
        _assert_component_refs_resolve(child, root, f"{location}.{key}")


def _validate_documents(
    inputs: list[tuple[str, dict[str, Any]]],
) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    if not inputs:
        raise ValueError("at least one OpenAPI document is required")
    documents = sorted(inputs, key=lambda item: item[0])
    sources = [source for source, _ in documents]
    if any(not isinstance(source, str) or not source for source in sources):
        raise ValueError("OpenAPI source identity must be a non-empty string")
    if len(sources) != len(set(sources)):
        raise ValueError("OpenAPI source identities must be unique")
    for source, document in documents:
        if not isinstance(document, dict):
            raise ValueError(f"{source} must contain an OpenAPI object")
        validate_json_data(document, source)
        if not isinstance(document.get("openapi"), str):
            raise ValueError(f"{source} has no OpenAPI version")
        if not isinstance(document.get("paths", {}), dict):
            raise ValueError(f"{source} paths must be an object")
    versions = {document["openapi"] for _, document in documents}
    if len(versions) != 1:
        raise ValueError("OpenAPI input versions conflict")
    return documents, versions.pop()


def _emit_components(
    inventory: dict[str, dict[str, dict[str, Any]]],
    names: dict[str, dict[str, dict[str, str]]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, list[dict[str, Any]]]]]:
    components: dict[str, dict[str, Any]] = {}
    all_variants: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for category, entries in sorted(inventory.items()):
        emitted, variants = _emit_component_category(category, entries, names)
        if emitted:
            components[category] = emitted
        if variants:
            all_variants[category] = variants
    return components, all_variants


def _emit_component_category(
    category: str,
    entries: dict[str, dict[str, Any]],
    names: dict[str, dict[str, dict[str, str]]],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    emitted_entries: dict[str, Any] = {}
    category_variants: dict[str, list[dict[str, Any]]] = {}
    for name, values in sorted(entries.items()):
        rewritten = {
            source: _rewrite_refs(value, names[source]) for source, value in values.items()
        }
        groups = _group_values(rewritten)
        variants = _emit_component_groups(
            category,
            name,
            groups,
            names,
            emitted_entries,
        )
        if len(groups) > 1:
            category_variants[name] = variants
    return dict(sorted(emitted_entries.items())), category_variants


def _emit_component_groups(
    category: str,
    name: str,
    groups: list[tuple[list[str], Any]],
    names: dict[str, dict[str, dict[str, str]]],
    emitted_entries: dict[str, Any],
) -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    for sources, value in groups:
        emitted_name = names[sources[0]][category][name]
        previous = emitted_entries.get(emitted_name)
        if previous is not None and _canonical(previous) != _canonical(value):
            raise ValueError(f"component emitted-name collision: {category}.{emitted_name}")
        emitted_entries[emitted_name] = value
        variants.append({"sources": sources, "emitted_name": emitted_name})
    return variants


def _merge_paths(
    documents: list[tuple[str, dict[str, Any]]],
    names: dict[str, dict[str, dict[str, str]]],
    common_security: Any,
    common_servers: Any,
) -> dict[str, Any]:
    inventory: dict[str, dict[str, Any]] = defaultdict(dict)
    for source, document in documents:
        for path, path_item in document.get("paths", {}).items():
            preserved = _preserve_inherited_root_semantics(
                path_item,
                document,
                common_security,
                common_servers,
            )
            inventory[path][source] = _rewrite_refs(preserved, names[source])
    return {path: _emit_path_values(values) for path, values in sorted(inventory.items())}


def _emit_path_values(values: dict[str, Any]) -> Any:
    groups = _group_values(values)
    if len(groups) == 1:
        return groups[0][1]
    return {
        "x-f5-path-variants": [
            {"source": source, "value": values[source]} for source in sorted(values)
        ]
    }


def _domain_metadata(
    documents: list[tuple[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    return {
        source: {
            key: deepcopy(value)
            for key, value in document.items()
            if key not in {"paths", "components"}
        }
        for source, document in documents
    }


def _merged_tags(documents: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    unique: dict[bytes, Any] = {}
    for source, document in documents:
        tags = document.get("tags", [])
        if not isinstance(tags, list):
            raise ValueError(f"{source} tags must be an array")
        for tag in tags:
            unique[_canonical(tag)] = deepcopy(tag)
    return [unique[key] for key in sorted(unique)]


def merge_openapi_documents(
    inputs: list[tuple[str, dict[str, Any]]],
    *,
    title: str,
    version: str,
) -> dict[str, Any]:
    """Merge inputs without silently selecting a path or component winner."""
    documents, openapi_version = _validate_documents(inputs)
    inventory = _component_inventory(documents)
    names = _component_names(documents, inventory)
    components, component_variants = _emit_components(inventory, names)
    common_security = _common_root_value(documents, "security")
    common_servers = _common_root_value(documents, "servers")
    aggregate: dict[str, Any] = {
        "openapi": openapi_version,
        "info": {"title": title, "version": version},
        "paths": _merge_paths(documents, names, common_security, common_servers),
        "components": components,
        "x-f5-domain-metadata": _domain_metadata(documents),
    }
    if common_security is not None:
        aggregate["security"] = common_security
    if common_servers is not None:
        aggregate["servers"] = common_servers
    tags = _merged_tags(documents)
    if tags:
        aggregate["tags"] = tags
    if component_variants:
        aggregate["x-f5-component-variants"] = component_variants
    _assert_component_refs_resolve(aggregate, aggregate)
    return aggregate
