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

from .utils.spec_loader import save_spec_to_file

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


def _build_spelling_patterns(
    corrections: dict[str, str],
) -> list[tuple[re.Pattern[str], str]]:
    """Pre-compile word-boundary regex patterns for each correction.

    Longer typos are matched first to prevent shorter substrings from
    clobbering partial matches (e.g. ``Addresss`` before ``addres``).
    """
    patterns = []
    for typo in sorted(corrections, key=len, reverse=True):
        fix = corrections[typo]
        pattern = re.compile(r"(?<!\w)" + re.escape(typo) + r"(?!\w)")
        patterns.append((pattern, fix))
    return patterns


def _fix_spelling_recursive(
    obj: Any, patterns: list[tuple[re.Pattern[str], str]]
) -> None:
    """Walk *obj* in-place and fix known spelling errors in text fields."""
    if isinstance(obj, dict):
        for key in ("description", "summary", "title"):
            if key in obj and isinstance(obj[key], str):
                text = obj[key]
                for pattern, fix in patterns:
                    text = pattern.sub(fix, text)
                obj[key] = text
        for value in obj.values():
            _fix_spelling_recursive(value, patterns)
    elif isinstance(obj, list):
        for item in obj:
            _fix_spelling_recursive(item, patterns)


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
    version = config.metadata.get("spec_date") or config.metadata.get(
        "download_date", ""
    )
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
    prefix = "#/components/schemas/"
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
    """Rename misspelled JSON property keys in component schemas.

    Only applies corrections marked ``verified: true`` in the config.
    """
    corrections = config.metadata.get("property_name_corrections", [])
    if not corrections:
        return spec

    schemas = spec.get("components", {}).get("schemas", {})
    for rule in corrections:
        if not rule.get("verified", False):
            continue
        schema_name = rule["schema"]
        old_key = rule["old_key"]
        new_key = rule["new_key"]

        schema_def = schemas.get(schema_name)
        if schema_def is None:
            continue

        props = schema_def.get("properties", {})
        if old_key not in props:
            continue

        props[new_key] = props.pop(old_key)

        required = schema_def.get("required", [])
        if old_key in required:
            required[required.index(old_key)] = new_key

    return spec


@register_transform("fix_spelling")
def fix_spelling(
    spec: dict,
    config: TransformConfig,
    _filename: str,
) -> dict:
    """Fix known spelling errors in description, summary, and title fields."""
    corrections = config.metadata.get("spelling_corrections", {})
    if not corrections:
        return spec
    patterns = _build_spelling_patterns(corrections)
    _fix_spelling_recursive(spec, patterns)
    return spec


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
            console.print(
                f"  [dim]{result.filename}: {len(result.changes)} changes[/dim]"
            )

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

    metadata: dict[str, Any] = {}
    metadata_path = input_dir / ".spec_metadata.json"
    if metadata_path.exists():
        with metadata_path.open() as fh:
            metadata = json.load(fh)

    spelling_path = config_path.parent / "spelling_corrections.yaml"
    if spelling_path.exists():
        with spelling_path.open() as fh:
            spelling_cfg = yaml.safe_load(fh) or {}
        metadata["spelling_corrections"] = spelling_cfg.get("corrections", {})

    property_path = config_path.parent / "property_name_corrections.yaml"
    if property_path.exists():
        with property_path.open() as fh:
            property_cfg = yaml.safe_load(fh) or {}
        metadata["property_name_corrections"] = property_cfg.get("corrections", [])

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
