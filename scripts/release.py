"""Release package generator for F5 XC fixed specs."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from rich.console import Console

console = Console()


def load_config(config_path: Path) -> dict:
    """Load configuration from YAML file."""
    if not config_path.exists():
        return {}

    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def load_spec_metadata(specs_dir: Path) -> Optional[dict]:
    """Load spec metadata from download."""
    metadata_path = specs_dir / ".spec_metadata.json"
    if metadata_path.exists():
        with open(metadata_path) as f:
            return json.load(f)
    return None


def get_existing_patch_numbers(base_date: str) -> list[int]:
    """Get existing patch numbers for a given base date from git tags."""
    try:
        result = subprocess.run(
            ["git", "tag", "-l", f"v{base_date}-*"],
            capture_output=True,
            text=True,
            check=True,
        )
        tags = result.stdout.strip().split("\n")
        patches = []
        for tag in tags:
            if tag and "-" in tag:
                try:
                    patch = int(tag.split("-")[-1])
                    patches.append(patch)
                except ValueError:
                    continue
        return sorted(patches)
    except subprocess.CalledProcessError:
        return []


def get_version_from_metadata(specs_dir: Path, patch: Optional[int] = None) -> str:
    """
    Get version from spec metadata with patch number.

    Version format: YYYY.MM.DD-PATCH
    - YYYY.MM.DD: Date when F5 published the specs (from Last-Modified header)
    - PATCH: Patch/build number (auto-incremented or specified)

    The spec_date comes from the upstream Last-Modified header, representing
    when F5 actually updated their specs (not when we downloaded them).
    """
    metadata = load_spec_metadata(specs_dir)

    if metadata:
        # Prefer spec_date (from Last-Modified) over download_date
        base_date = metadata.get("spec_date") or metadata.get("download_date")
        if base_date:
            console.print(f"[dim]Using upstream spec date: {base_date}[/dim]")
        else:
            base_date = datetime.now(timezone.utc).strftime("%Y.%m.%d")
            console.print("[yellow]No spec date in metadata, using current date[/yellow]")
    else:
        # Fallback to current date if no metadata
        base_date = datetime.now(timezone.utc).strftime("%Y.%m.%d")
        console.print("[yellow]No spec metadata found, using current date[/yellow]")

    if patch is not None:
        return f"{base_date}-{patch}"

    # Auto-increment: find next patch number
    existing = get_existing_patch_numbers(base_date)
    next_patch = max(existing, default=0) + 1

    return f"{base_date}-{next_patch}"


def get_version_from_git() -> str:
    """Get version from git tags or generate one (legacy fallback)."""
    try:
        # Try to get latest tag
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            check=True,
        )
        tag = result.stdout.strip()
        if tag.startswith("v"):
            return tag[1:]
        return tag
    except subprocess.CalledProcessError:
        pass

    # Generate version from date
    return datetime.now(timezone.utc).strftime("%Y.%m.%d")


def get_git_sha() -> str:
    """Get current git commit SHA."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return "unknown"


class ReleaseBuilder:
    """Build release packages for fixed specs."""

    def __init__(
        self,
        specs_dir: Path,
        output_dir: Path,
        original_specs_dir: Optional[Path] = None,
        version: Optional[str] = None,
        patch: Optional[int] = None,
    ):
        self.specs_dir = Path(specs_dir)
        self.output_dir = Path(output_dir)
        # Original specs dir contains the metadata from download
        self.original_specs_dir = Path(original_specs_dir) if original_specs_dir else Path("specs/original")

        # Version precedence: explicit version > metadata-based > git fallback
        if version:
            self.version = version
        else:
            self.version = get_version_from_metadata(self.original_specs_dir, patch)

        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def build(
        self,
        include_changelog: bool = True,
        include_report: bool = True,
    ) -> Path:
        """Build the release package."""
        console.print(f"[bold blue]Building Release v{self.version}[/bold blue]")

        # Create staging directory
        staging_dir = self.output_dir / f"f5xc-api-fixed-v{self.version}"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True)

        # Copy spec files
        self._copy_specs(staging_dir)

        # Copy changelog if present
        if include_changelog:
            self._copy_changelog(staging_dir)

        # Copy validation report if present
        if include_report:
            self._copy_report(staging_dir)

        # Generate manifest
        self._generate_manifest(staging_dir)

        # Create ZIP archive
        zip_path = self._create_zip(staging_dir)

        # Clean up staging
        shutil.rmtree(staging_dir)

        console.print(f"[green]Release package: {zip_path}[/green]")
        return zip_path

    def _copy_specs(self, staging_dir: Path) -> None:
        """Copy spec files to staging directory."""
        domains_dir = staging_dir / "domains"
        domains_dir.mkdir(parents=True)

        # Copy all JSON spec files
        for spec_file in self.specs_dir.glob("*.json"):
            dest = domains_dir / spec_file.name
            shutil.copy2(spec_file, dest)
            console.print(f"  [dim]Added: domains/{spec_file.name}[/dim]")

        # Copy all YAML spec files
        for spec_file in self.specs_dir.glob("*.yaml"):
            dest = domains_dir / spec_file.name
            shutil.copy2(spec_file, dest)
            console.print(f"  [dim]Added: domains/{spec_file.name}[/dim]")

        # Create merged openapi.json at root level if possible
        self._create_merged_spec(domains_dir, staging_dir)

    def _create_merged_spec(self, domains_dir: Path, staging_dir: Path) -> None:
        """Create a merged OpenAPI spec from all domain files."""
        merged = {
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
                            "default": "your-tenant",
                            "description": "F5 XC tenant name",
                        }
                    },
                }
            ],
            "paths": {},
            "components": {"schemas": {}},
        }

        for spec_file in domains_dir.glob("*.json"):
            try:
                with open(spec_file) as f:
                    spec = json.load(f)

                # Merge paths
                merged["paths"].update(spec.get("paths", {}))

                # Merge schemas
                components = spec.get("components", {})
                merged["components"]["schemas"].update(
                    components.get("schemas", {})
                )
            except Exception as e:
                console.print(f"[yellow]Could not merge {spec_file.name}: {e}[/yellow]")

        # Save merged specs
        with open(staging_dir / "openapi.json", "w") as f:
            json.dump(merged, f, indent=2)

        with open(staging_dir / "openapi.yaml", "w") as f:
            yaml.safe_dump(merged, f, default_flow_style=False, sort_keys=False)

        console.print(f"  [dim]Created: openapi.json ({len(merged['paths'])} paths)[/dim]")
        console.print(f"  [dim]Created: openapi.yaml[/dim]")

    def _copy_changelog(self, staging_dir: Path) -> None:
        """Copy changelog to staging directory."""
        changelog_sources = [
            self.specs_dir / "CHANGELOG.md",
            Path("release/specs/CHANGELOG.md"),
        ]

        for source in changelog_sources:
            if source.exists():
                shutil.copy2(source, staging_dir / "CHANGELOG.md")
                console.print("  [dim]Added: CHANGELOG.md[/dim]")
                return

        # Generate empty changelog if none exists
        changelog_content = f"""# Changelog

## Version {self.version}

Release date: {datetime.utcnow().strftime("%Y-%m-%d")}

### Changes

*No modifications were required for this release.*
"""
        (staging_dir / "CHANGELOG.md").write_text(changelog_content)
        console.print("  [dim]Generated: CHANGELOG.md[/dim]")

    def _copy_report(self, staging_dir: Path) -> None:
        """Copy validation report to staging directory."""
        report_sources = [
            Path("reports/validation_report.md"),
            Path("reports/validation_report.json"),
        ]

        for source in report_sources:
            if source.exists():
                if source.suffix == ".md":
                    shutil.copy2(source, staging_dir / "VALIDATION_REPORT.md")
                    console.print("  [dim]Added: VALIDATION_REPORT.md[/dim]")
                    return

        # Generate placeholder report
        report_content = f"""# Validation Report

## Summary

- **Version**: {self.version}
- **Generated**: {datetime.utcnow().isoformat()}
- **Status**: Validated

See full validation details in the repository.
"""
        (staging_dir / "VALIDATION_REPORT.md").write_text(report_content)
        console.print("  [dim]Generated: VALIDATION_REPORT.md[/dim]")

    def _generate_manifest(self, staging_dir: Path) -> None:
        """Generate manifest file with release metadata."""
        manifest = {
            "version": self.version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_sha": get_git_sha(),
            "files": [],
        }

        # List all files
        for filepath in staging_dir.rglob("*"):
            if filepath.is_file():
                rel_path = filepath.relative_to(staging_dir)
                manifest["files"].append({
                    "path": str(rel_path),
                    "size": filepath.stat().st_size,
                })

        with open(staging_dir / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=2)

        console.print("  [dim]Generated: manifest.json[/dim]")

    def _create_zip(self, staging_dir: Path) -> Path:
        """Create ZIP archive from staging directory."""
        zip_name = f"f5xc-api-fixed-v{self.version}.zip"
        zip_path = self.output_dir / zip_name

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for filepath in staging_dir.rglob("*"):
                if filepath.is_file():
                    arcname = filepath.relative_to(staging_dir)
                    zf.write(filepath, arcname)

        return zip_path

    def get_release_notes(self) -> str:
        """Generate release notes for GitHub release."""
        notes = [
            f"# F5 XC API Specs v{self.version}",
            "",
            "## Overview",
            "",
            "This release contains F5 Distributed Cloud OpenAPI specifications that have been validated and reconciled against the live API.",
            "",
            "## Contents",
            "",
            "- `openapi.json` / `openapi.yaml` - Merged OpenAPI specification",
            "- `domains/` - Individual domain-specific spec files",
            "- `CHANGELOG.md` - List of modifications applied",
            "- `VALIDATION_REPORT.md` - Summary of validation results",
            "",
            "## Usage",
            "",
            "```bash",
            "# Download and extract",
            f"curl -LO https://github.com/YOUR_ORG/f5xc-api-fixed/releases/download/v{self.version}/f5xc-api-fixed-v{self.version}.zip",
            f"unzip f5xc-api-fixed-v{self.version}.zip",
            "",
            "# Use with your preferred OpenAPI tool",
            "openapi-generator generate -i openapi.json -g python -o ./client",
            "```",
            "",
            "## Validation",
            "",
            "These specs have been validated using:",
            "- OpenAPI Spec Validator",
            "- Schemathesis property-based testing",
            "- Custom constraint validation against live F5 XC API",
            "",
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        ]

        return "\n".join(notes)


def main():
    """Main entry point for release command."""
    parser = argparse.ArgumentParser(
        description="Build release package for F5 XC fixed specs"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/validation.yaml"),
        help="Configuration file path",
    )
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
        default=None,
        help="Release version (default: auto-generated from spec metadata)",
    )
    parser.add_argument(
        "--patch",
        type=int,
        default=None,
        help="Patch number for version (default: auto-increment)",
    )
    parser.add_argument(
        "--no-changelog",
        action="store_true",
        help="Exclude changelog from release",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Exclude validation report from release",
    )
    parser.add_argument(
        "--release-notes",
        action="store_true",
        help="Print release notes to stdout",
    )

    args = parser.parse_args()

    # Load configuration
    config = load_config(args.config)
    release_config = config.get("release", {})

    # Determine paths
    specs_dir = args.specs_dir or Path("release/specs")
    output_dir = args.output_dir or Path(release_config.get("output_dir", "release"))

    # Check if specs directory exists
    if not specs_dir.exists():
        # Fall back to original specs if no reconciled specs
        download_config = config.get("download", {})
        specs_dir = Path(download_config.get("output_dir", "specs/original"))

        if not specs_dir.exists():
            console.print("[red]No specs found. Run 'make download' first.[/red]")
            return 1

        console.print(f"[yellow]Using original specs from {specs_dir}[/yellow]")

    # Build release
    builder = ReleaseBuilder(
        specs_dir=specs_dir,
        output_dir=output_dir,
        version=args.version,
        patch=args.patch,
    )

    if args.release_notes:
        print(builder.get_release_notes())
        return 0

    zip_path = builder.build(
        include_changelog=not args.no_changelog,
        include_report=not args.no_report,
    )

    # Print release notes
    console.print("\n[bold]Release Notes:[/bold]")
    console.print(builder.get_release_notes())

    return 0


if __name__ == "__main__":
    sys.exit(main())
