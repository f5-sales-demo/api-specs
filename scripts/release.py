"""Release package generator for F5 XC fixed specs."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console

from scripts.utils.constraint_validator import Discrepancy, DiscrepancyType
from scripts.utils.discrepancy_fingerprint import fingerprint

console = Console()

BUILD_TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
VERSION_PATTERN = re.compile(r"^(?P<date>[0-9]{4}\.[0-9]{2}\.[0-9]{2})-(?P<patch>[1-9][0-9]*)$")
ZIP_MINIMUM_YEAR = 1980
ZIP_MAXIMUM_YEAR = 2107
CANONICAL_FILE_MODE = 0o100644
CANONICAL_COMPRESSION_LEVEL = 9


def parse_build_timestamp(value: str) -> datetime:
    """Parse an explicit, timezone-aware release build timestamp."""
    if not isinstance(value, str) or BUILD_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError("build timestamp must be an ISO 8601 timestamp with a UTC offset")

    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("build timestamp is not a valid ISO 8601 timestamp") from exc

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("build timestamp must include a UTC offset")

    timestamp = timestamp.astimezone(UTC)
    if not ZIP_MINIMUM_YEAR <= timestamp.year <= ZIP_MAXIMUM_YEAR:
        raise ValueError(
            f"build timestamp year must be between {ZIP_MINIMUM_YEAR} and {ZIP_MAXIMUM_YEAR}"
        )
    return timestamp


def validate_version(value: str) -> str:
    """Require the one canonical release-version grammar used by the workflow."""
    if not isinstance(value, str):
        raise ValueError("release version must use YYYY.MM.DD-N")
    match = VERSION_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("release version must use YYYY.MM.DD-N")
    try:
        datetime.strptime(match.group("date"), "%Y.%m.%d")
    except ValueError as error:
        raise ValueError("release version contains an invalid date") from error
    return value


def build_validation_report_md(
    validation_report_json: Path,
    generated_at: str,
    issue_mapping_json: Path | None = None,
) -> str:
    """Assemble VALIDATION_REPORT.md, including a Tracked as issues column.

    Reads the JSON validation report produced by
    :class:`scripts.utils.report_generator.ReportGenerator` and, when
    ``issue_mapping_json`` points to an existing file, annotates each
    discrepancy row with the GitHub issue it has been tracked as. Rows
    without a matching mapping entry render an em-dash.

    Returns the markdown document as a string; the caller is responsible
    for writing it to disk.
    """
    data = json.loads(Path(validation_report_json).read_text(encoding="utf-8"))

    mapping: dict[str, dict[str, Any]] = {}
    if issue_mapping_json is not None:
        mapping_path = Path(issue_mapping_json)
        if mapping_path.exists():
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))

    summary = data.get("summary", {}) or {}
    discrepancies = data.get("discrepancies", []) or []

    lines: list[str] = [
        "# F5 XC API Validation Report",
        "",
        f"**Generated:** {generated_at}",
        "",
    ]

    if summary:
        lines.extend(
            [
                "## Summary",
                "",
                f"- **Total Endpoints:** {summary.get('total_endpoints', 0)}",
                f"- **Total Tests:** {summary.get('total_tests', 0)}",
                f"- **Passed:** {summary.get('passed', 0)}",
                f"- **Failed:** {summary.get('failed', 0)}",
                f"- **Errors:** {summary.get('errors', 0)}",
                "- **Discrepancies Found:** "
                f"{summary.get('total_discrepancies', len(discrepancies))}",
                "",
            ]
        )

    lines.extend(
        [
            "## Discrepancies",
            "",
            "| Path | Property | Constraint | Type | Tracked as issues |",
            "|------|----------|------------|------|-------------------|",
        ]
    )

    for d in discrepancies:
        try:
            disc = Discrepancy(
                path=d["path"],
                property_name=d["property_name"],
                constraint_type=d["constraint_type"],
                discrepancy_type=DiscrepancyType(d["discrepancy_type"]),
                spec_value=d.get("spec_value"),
                api_behavior=d.get("api_behavior"),
                test_values=d.get("test_values", []) or [],
            )
        except (KeyError, ValueError):
            # Malformed entry — skip rather than abort the report.
            continue

        fp = fingerprint(
            disc,
            d.get("domain", "unknown"),
            d.get("method", "unknown"),
        )
        entry = mapping.get(fp)
        if entry and entry.get("issue_number") and entry.get("issue_url"):
            issue_cell = f"[#{entry['issue_number']}]({entry['issue_url']})"
        else:
            issue_cell = "—"

        lines.append(
            f"| `{disc.path}` | `{disc.property_name}` | "
            f"{disc.constraint_type} | {disc.discrepancy_type.value} | "
            f"{issue_cell} |"
        )

    lines.append("")
    return "\n".join(lines)


def get_git_sha() -> str:
    """Return the full source commit SHA, failing when provenance is unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("release source commit could not be resolved") from error

    commit = result.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise RuntimeError("release source commit is not a full Git SHA")
    return commit


class ReleaseBuilder:
    """Build release packages for fixed specs."""

    def __init__(
        self,
        specs_dir: Path,
        output_dir: Path,
        version: str,
        build_timestamp: str,
    ) -> None:
        """Initialize ReleaseBuilder with paths and version info."""
        self.specs_dir = Path(specs_dir)
        self.output_dir = Path(output_dir)
        self.version = validate_version(version)
        self.build_datetime = parse_build_timestamp(build_timestamp)
        self.build_timestamp = self.build_datetime.isoformat()

        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def build(self) -> Path:
        """Build the release package."""
        console.print(f"[bold blue]Building Release v{self.version}[/bold blue]")

        zip_path = self.artifact_path()
        if zip_path.is_symlink() or (zip_path.exists() and not zip_path.is_file()):
            raise ValueError(f"release artifact path is unsafe: {zip_path}")
        zip_path.unlink(missing_ok=True)

        # Create staging directory
        staging_dir = self.output_dir / f"api-specs-v{self.version}"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True)
        try:
            self._copy_specs(staging_dir)
            self._copy_changelog(staging_dir)
            self._copy_report(staging_dir)
            self._generate_manifest(staging_dir)
            self._create_zip(staging_dir)
        finally:
            shutil.rmtree(staging_dir)

        console.print(f"[green]Release package: {zip_path}[/green]")
        return zip_path

    def artifact_path(self) -> Path:
        """Return the exact ZIP path produced for this release."""
        return self.output_dir / f"api-specs-v{self.version}.zip"

    def _copy_specs(self, staging_dir: Path) -> None:
        """Copy spec files to staging directory."""
        domains_dir = staging_dir / "domains"
        domains_dir.mkdir(parents=True)

        # Download metadata is provenance for the build clock, not a domain.
        json_specs = sorted(
            path for path in self.specs_dir.glob("*.json") if not path.name.startswith(".")
        )
        if not json_specs:
            raise FileNotFoundError(f"no JSON domain specs found in {self.specs_dir}")

        for spec_file in json_specs:
            if spec_file.is_symlink() or not spec_file.is_file():
                raise ValueError(f"domain spec path is unsafe: {spec_file}")
            dest = domains_dir / spec_file.name
            shutil.copy2(spec_file, dest)
            console.print(f"  [dim]Added: domains/{spec_file.name}[/dim]")

        # Copy all YAML spec files
        for spec_file in sorted(
            path for path in self.specs_dir.glob("*.yaml") if not path.name.startswith(".")
        ):
            if spec_file.is_symlink() or not spec_file.is_file():
                raise ValueError(f"domain spec path is unsafe: {spec_file}")
            dest = domains_dir / spec_file.name
            shutil.copy2(spec_file, dest)
            console.print(f"  [dim]Added: domains/{spec_file.name}[/dim]")

        # Create merged openapi.json at root level if possible
        self._create_merged_spec(domains_dir, staging_dir)

    def _create_merged_spec(self, domains_dir: Path, staging_dir: Path) -> None:
        """Create a merged OpenAPI spec from all domain files."""
        merged: dict[str, Any] = {
            "openapi": "3.0.0",
            "info": {
                "title": "F5 Distributed Cloud API (Fixed)",
                "version": self.version,
                "description": "Reconciled F5 XC OpenAPI specification",
            },
            "servers": [
                {
                    "url": "https://{tenant}.console.ves.volterra.io",
                    "variables": {
                        "tenant": {
                            "default": "example-tenant",
                            "description": "F5 XC tenant name",
                        }
                    },
                }
            ],
            "paths": {},
            "components": {"schemas": {}},
        }

        for spec_file in sorted(domains_dir.glob("*.json")):
            try:
                with spec_file.open(encoding="utf-8") as f:
                    spec = json.load(f)
            except (json.JSONDecodeError, OSError) as error:
                raise ValueError(f"domain spec cannot be read: {spec_file.name}") from error
            if not isinstance(spec, dict):
                raise ValueError(f"domain spec is not an object: {spec_file.name}")

            paths = spec.get("paths", {})
            components = spec.get("components", {})
            if not isinstance(paths, dict) or not isinstance(components, dict):
                raise ValueError(f"domain spec has invalid OpenAPI objects: {spec_file.name}")
            schemas = components.get("schemas", {})
            if not isinstance(schemas, dict):
                raise ValueError(f"domain spec has invalid schemas: {spec_file.name}")

            merged["paths"].update(paths)
            merged["components"]["schemas"].update(schemas)

        # Save merged specs
        with (staging_dir / "openapi.json").open("w") as f:
            json.dump(merged, f, indent=2)

        with (staging_dir / "openapi.yaml").open("w") as f:
            yaml.safe_dump(merged, f, default_flow_style=False, sort_keys=False)

        console.print(f"  [dim]Created: openapi.json ({len(merged['paths'])} paths)[/dim]")
        console.print("  [dim]Created: openapi.yaml[/dim]")

    def _copy_changelog(self, staging_dir: Path) -> None:
        """Copy reconciliation evidence, failing when it is absent."""
        source = self.specs_dir / "CHANGELOG.md"
        if source.is_symlink() or not source.is_file():
            raise FileNotFoundError(f"required CHANGELOG.md is missing from {self.specs_dir}")
        shutil.copy2(source, staging_dir / "CHANGELOG.md")
        console.print("  [dim]Added: CHANGELOG.md[/dim]")

    def _copy_report(self, staging_dir: Path) -> None:
        """Copy validation report to staging directory.

        Rebuild the markdown from JSON evidence so its generated timestamp is
        the immutable release build timestamp. Missing evidence fails closed.
        """
        validation_json = Path("reports/validation_report.json")
        issue_mapping = Path("reports/issue_mapping.json")

        if validation_json.is_symlink():
            raise ValueError("validation report path must not be a symlink")
        if validation_json.is_file():
            if issue_mapping.is_symlink() or (
                issue_mapping.exists() and not issue_mapping.is_file()
            ):
                raise ValueError("issue mapping path must be a regular non-symlink file")
            report_content = build_validation_report_md(
                validation_json,
                self.build_timestamp,
                issue_mapping if issue_mapping.exists() else None,
            )
            (staging_dir / "VALIDATION_REPORT.md").write_text(
                report_content,
                encoding="utf-8",
            )
            console.print("  [dim]Generated: VALIDATION_REPORT.md (with issue tracking)[/dim]")
            return

        raise FileNotFoundError("required validation report is missing")

    def _generate_manifest(self, staging_dir: Path) -> None:
        """Generate manifest file with release metadata."""
        manifest: dict[str, Any] = {
            "version": self.version,
            "generated_at": self.build_timestamp,
            "git_sha": get_git_sha(),
            "files": [],
        }

        # List all files
        for filepath in sorted(staging_dir.rglob("*")):
            if filepath.is_file():
                rel_path = filepath.relative_to(staging_dir)
                manifest["files"].append(
                    {
                        "path": rel_path.as_posix(),
                        "size": filepath.stat().st_size,
                    }
                )

        with (staging_dir / "manifest.json").open("w") as f:
            json.dump(manifest, f, indent=2)

        console.print("  [dim]Generated: manifest.json[/dim]")

    def _create_zip(self, staging_dir: Path) -> Path:
        """Atomically create a ZIP with canonical member metadata and order."""
        zip_path = self.artifact_path()

        descriptor, temporary_name = tempfile.mkstemp(
            dir=zip_path.parent,
            prefix=f".{zip_path.name}.",
            suffix=".tmp",
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            with zipfile.ZipFile(
                temporary_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=CANONICAL_COMPRESSION_LEVEL,
            ) as archive:
                for filepath in sorted(staging_dir.rglob("*")):
                    if not filepath.is_file():
                        continue
                    arcname = filepath.relative_to(staging_dir).as_posix()
                    member = zipfile.ZipInfo(
                        filename=arcname,
                        date_time=self.build_datetime.timetuple()[:6],
                    )
                    member.compress_type = zipfile.ZIP_DEFLATED
                    member.create_system = 3
                    member.external_attr = CANONICAL_FILE_MODE << 16
                    member.internal_attr = 0
                    member.extra = b""
                    member.comment = b""
                    archive.writestr(
                        member,
                        filepath.read_bytes(),
                        compress_type=zipfile.ZIP_DEFLATED,
                        compresslevel=CANONICAL_COMPRESSION_LEVEL,
                    )
            os.replace(temporary_path, zip_path)
        finally:
            temporary_path.unlink(missing_ok=True)

        return zip_path


def main() -> int:
    """Main entry point for release command."""
    parser = argparse.ArgumentParser(description="Build release package for F5 XC fixed specs")
    parser.add_argument(
        "--specs-dir",
        type=Path,
        default=None,
        help="Directory containing reconciled specs",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for release package",
    )
    parser.add_argument(
        "--version",
        type=str,
        required=True,
        help="Exact release version",
    )
    parser.add_argument(
        "--build-timestamp",
        type=str,
        required=True,
        help="Immutable ISO 8601 timestamp from spec metadata",
    )

    args = parser.parse_args()

    specs_dir = args.specs_dir or Path("release/specs")
    output_dir = args.output_dir or Path("release")

    # Check if specs directory exists
    if not specs_dir.exists():
        console.print("[red]Reconciled release specs are missing.[/red]")
        return 1

    # Build release
    builder = ReleaseBuilder(
        specs_dir=specs_dir,
        output_dir=output_dir,
        version=args.version,
        build_timestamp=args.build_timestamp,
    )
    builder.build()

    return 0


if __name__ == "__main__":
    sys.exit(main())
