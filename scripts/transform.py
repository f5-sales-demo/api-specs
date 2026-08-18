"""OAS3 spec transform pipeline -- clean upstream specs before validation."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from rich.console import Console

from .transforms.corrections import (
    FIX_SPEC_STATUS,
    ONEOF_FIELD_PREFIX,
    SCHEMA_REF_PREFIX,
    WIRE_NAME_EXTENSION,
    _collect_refs,
    find_dangling_refs,
    fix_dangling_refs,
    fix_invalid_examples,
    fix_oneof_group_names,
    fix_property_names,
    fix_spelling,
    rename_colliding_schemas,
    sanitize_example_identifiers,
    sanitize_example_placeholders,
    sanitize_pii_placeholders,
)
from .utils.nullable_response import apply_nullable_response_corrections
from .utils.spec_loader import save_spec_to_file
from .utils.spec_sanitizers import strip_scripts_recursive
from .utils.text_replacements import (
    DEFAULT_EXAMPLE_PLACEHOLDER_FIELDS,
    DEFAULT_SPELLING_TEXT_FIELDS,
)

if TYPE_CHECKING:
    from collections.abc import Callable

console = Console()
HTTP_METHODS: frozenset[str] = frozenset(
    {"get", "post", "put", "delete", "patch", "options", "head", "trace"}
)
_TAG_SEGMENT_INDEX = 1

__all__ = [
    "FIX_SPEC_STATUS",
    "HTTP_METHODS",
    "ONEOF_FIELD_PREFIX",
    "SCHEMA_REF_PREFIX",
    "TRANSFORM_REGISTRY",
    "WIRE_NAME_EXTENSION",
    "SpecTransformer",
    "TransformConfig",
    "TransformResult",
    "deduplicate_operation_ids",
    "find_dangling_refs",
    "fix_dangling_refs",
    "fix_invalid_examples",
    "fix_oneof_group_names",
    "fix_property_names",
    "fix_spelling",
    "inject_contact",
    "inject_info_version",
    "inject_operation_descriptions",
    "inject_operation_tags",
    "inject_security_schemes",
    "inject_servers",
    "load_config",
    "load_spec_metadata",
    "mark_deprecated_operations",
    "mark_nullable_response_fields",
    "remove_deprecated_paths",
    "remove_unused_schemas",
    "rename_colliding_schemas",
    "sanitize_example_identifiers",
    "sanitize_example_placeholders",
    "sanitize_pii_placeholders",
    "strip_script_tags",
]


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


def inject_info_version(
    spec: dict,
    config: TransformConfig,
    _filename: str,
) -> dict:
    """Set ``info.version`` from pipeline metadata."""
    version = config.metadata.get("spec_date") or config.metadata.get("download_date", "")
    spec.setdefault("info", {})["version"] = version
    return spec


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


def strip_script_tags(
    spec: dict,
    _config: TransformConfig,
    _filename: str,
) -> dict:
    """Strip ``<script>`` tags from all ``description`` fields recursively."""
    strip_scripts_recursive(spec)
    return spec


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


# This explicit order is a compatibility contract: later transforms depend on
# results from earlier ones. Keep changes here deliberate and tested.
TRANSFORM_REGISTRY: list[tuple[str, Callable[..., dict]]] = [
    ("inject_info_version", inject_info_version),
    ("inject_contact", inject_contact),
    ("inject_servers", inject_servers),
    ("inject_security_schemes", inject_security_schemes),
    ("inject_operation_tags", inject_operation_tags),
    ("deduplicate_operation_ids", deduplicate_operation_ids),
    ("strip_script_tags", strip_script_tags),
    ("fix_invalid_examples", fix_invalid_examples),
    ("rename_colliding_schemas", rename_colliding_schemas),
    ("remove_deprecated_paths", remove_deprecated_paths),
    ("mark_deprecated_operations", mark_deprecated_operations),
    ("fix_dangling_refs", fix_dangling_refs),
    ("mark_nullable_response_fields", mark_nullable_response_fields),
    ("remove_unused_schemas", remove_unused_schemas),
    ("fix_property_names", fix_property_names),
    ("fix_oneof_group_names", fix_oneof_group_names),
    ("fix_spelling", fix_spelling),
    ("sanitize_example_placeholders", sanitize_example_placeholders),
    ("sanitize_example_identifiers", sanitize_example_identifiers),
    ("sanitize_pii_placeholders", sanitize_pii_placeholders),
    ("inject_operation_descriptions", inject_operation_descriptions),
]


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
