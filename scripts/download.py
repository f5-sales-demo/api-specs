"""Download and extract F5 XC OpenAPI specifications."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from pathlib import Path

import requests
import yaml
from rich.console import Console
from rich.progress import BarColumn, DownloadColumn, Progress, TransferSpeedColumn

console = Console()

# Default configuration
DEFAULT_DOWNLOAD_URL = (
    "https://docs.cloud.f5.com/docs-v2/downloads/f5-distributed-cloud-open-api.zip"
)
DEFAULT_OUTPUT_DIR = "specs/original"
DEFAULT_ETAG_CACHE = ".etag_cache"
DEFAULT_METADATA_FILE = ".spec_metadata.json"


def load_config(config_path: Path) -> dict:
    """Load configuration from YAML file."""
    if not config_path.exists():
        return {}

    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def get_cached_etag(cache_path: Path) -> str | None:
    """Get cached ETag from previous download."""
    if cache_path.exists():
        return cache_path.read_text().strip()
    return None


def save_etag(cache_path: Path, etag: str) -> None:
    """Save ETag to cache file."""
    cache_path.write_text(etag)


def save_metadata(
    output_dir: Path,
    etag: str | None,
    last_modified: str | None,
    file_count: int,
) -> None:
    """Save download metadata for versioning."""
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    metadata_path = output_dir / DEFAULT_METADATA_FILE

    # Parse Last-Modified header to get the upstream spec date
    spec_date = None
    spec_timestamp = None
    if last_modified:
        try:
            dt = parsedate_to_datetime(last_modified)
            spec_date = dt.strftime("%Y.%m.%d")
            spec_timestamp = dt.isoformat()
        except (ValueError, TypeError):
            pass

    # Fallback to download date if Last-Modified not available
    now = datetime.now(timezone.utc)
    metadata = {
        "spec_date": spec_date or now.strftime("%Y.%m.%d"),
        "spec_timestamp": spec_timestamp,
        "download_date": now.strftime("%Y.%m.%d"),
        "download_timestamp": now.isoformat(),
        "etag": etag,
        "last_modified": last_modified,
        "file_count": file_count,
        "source_url": DEFAULT_DOWNLOAD_URL,
    }

    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)

    console.print(f"[dim]Metadata saved: {metadata_path}[/dim]")
    if spec_date:
        console.print(f"[dim]Upstream spec date: {spec_date}[/dim]")


def load_metadata(output_dir: Path) -> dict | None:
    """Load download metadata for versioning."""
    metadata_path = output_dir / DEFAULT_METADATA_FILE
    if metadata_path.exists():
        with open(metadata_path) as f:
            return json.load(f)
    return None


def download_specs(
    url: str = DEFAULT_DOWNLOAD_URL,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    etag_cache: str | Path = DEFAULT_ETAG_CACHE,
    force: bool = False,
) -> tuple[bool, list[str]]:
    """
    Download and extract OpenAPI specs from F5.

    Returns:
        Tuple of (changed, list of extracted files)
        - changed: True if new specs were downloaded, False if unchanged
        - files: List of extracted file paths
    """
    output_dir = Path(output_dir)
    etag_cache = Path(etag_cache)

    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)

    # Check ETag for incremental updates
    cached_etag = get_cached_etag(etag_cache) if not force else None

    # Check if output directory has actual spec files
    existing_files = list(output_dir.glob("*.json"))
    if cached_etag and not existing_files:
        console.print(
            "[yellow]ETag cache exists but no spec files found, forcing download[/yellow]"
        )
        cached_etag = None

    headers = {}
    if cached_etag:
        headers["If-None-Match"] = cached_etag

    console.print(f"[blue]Downloading specs from: {url}[/blue]")

    try:
        response = requests.get(url, headers=headers, stream=True, timeout=60)

        # Not modified - use cached version
        if response.status_code == 304:
            console.print("[green]Specs unchanged (ETag match), using cached version[/green]")
            # Return existing files
            existing_files = [
                str(f.relative_to(output_dir)) for f in output_dir.glob("**/*") if f.is_file()
            ]
            return False, existing_files

        response.raise_for_status()

        # Get total size for progress bar
        total_size = int(response.headers.get("content-length", 0))

        # Download with progress
        content = io.BytesIO()
        with Progress(
            "[progress.description]{task.description}",
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Downloading...", total=total_size)

            for chunk in response.iter_content(chunk_size=8192):
                content.write(chunk)
                progress.update(task, advance=len(chunk))

        # Save new ETag and Last-Modified
        new_etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        if new_etag:
            save_etag(etag_cache, new_etag)
            console.print(f"[dim]ETag saved: {new_etag[:20]}...[/dim]")

        # Extract ZIP
        content.seek(0)
        extracted_files = extract_zip(content, output_dir)

        # Save metadata for versioning (includes upstream Last-Modified date)
        save_metadata(output_dir, new_etag, last_modified, len(extracted_files))

        console.print(f"[green]Extracted {len(extracted_files)} files to {output_dir}[/green]")
        return True, extracted_files

    except requests.exceptions.RequestException as e:
        console.print(f"[red]Download failed: {e}[/red]")
        raise


def extract_zip(content: io.BytesIO, output_dir: Path) -> list[str]:
    """Extract ZIP contents to output directory."""
    extracted = []

    with zipfile.ZipFile(content) as zf:
        # List contents
        for info in zf.infolist():
            if info.is_dir():
                continue

            # Extract file
            filename = Path(info.filename).name
            output_path = output_dir / filename

            with zf.open(info) as src, open(output_path, "wb") as dst:
                dst.write(src.read())

            extracted.append(filename)
            console.print(f"  [dim]Extracted: {filename}[/dim]")

    return extracted


def list_domain_files(output_dir: Path) -> dict[str, list[str]]:
    """List domain files and their contained paths."""
    domains = {}

    for filepath in output_dir.glob("*.json"):
        try:
            with open(filepath) as f:
                spec = json.load(f)

            paths = list(spec.get("paths", {}).keys())
            domains[filepath.name] = paths[:10]  # First 10 paths as preview

            console.print(f"[cyan]{filepath.name}[/cyan]: {len(paths)} paths")

        except Exception as e:
            console.print(f"[yellow]Failed to parse {filepath.name}: {e}[/yellow]")

    return domains


def compute_checksum(filepath: Path) -> str:
    """Compute SHA256 checksum of a file."""
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def main():
    """Main entry point for download command."""
    parser = argparse.ArgumentParser(description="Download F5 XC OpenAPI specifications")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/validation.yaml"),
        help="Configuration file path",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for specs",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force download even if cached",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_domains",
        help="List domain files after download",
    )

    args = parser.parse_args()

    # Load configuration
    config = load_config(args.config)
    download_config = config.get("download", {})

    # Determine paths
    url = download_config.get("url", DEFAULT_DOWNLOAD_URL)
    output_dir = args.output_dir or Path(download_config.get("output_dir", DEFAULT_OUTPUT_DIR))
    etag_cache = Path(download_config.get("etag_cache", DEFAULT_ETAG_CACHE))

    # Download specs
    changed, files = download_specs(
        url=url,
        output_dir=output_dir,
        etag_cache=etag_cache,
        force=args.force,
    )

    if args.list_domains:
        console.print("\n[bold]Domain Files:[/bold]")
        list_domain_files(output_dir)

    # Print checksums for verification
    console.print("\n[bold]File Checksums:[/bold]")
    for filename in sorted(files):
        filepath = output_dir / filename
        if filepath.exists():
            checksum = compute_checksum(filepath)
            console.print(f"  {filename}: {checksum[:16]}...")

    return 0 if files else 1


if __name__ == "__main__":
    exit(main())
