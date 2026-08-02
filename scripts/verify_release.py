"""Verify that a published GitHub release is sealed and byte-identical.

The downstream enrichment pipeline treats an API specification release as an
immutable input.  This verifier is deliberately fail-closed: the producer
workflow does not dispatch downstream until GitHub reports one final,
immutable release asset and the downloaded ZIP matches GitHub's SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import requests

GITHUB_API_VERSION = "2022-11-28"
SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class ReleaseVerificationError(RuntimeError):
    """Raised when a published release does not satisfy the release contract."""


def validate_release_metadata(
    release: dict[str, Any],
    repository: str,
    tag: str,
    expected_asset_name: str,
    expected_digest: str,
) -> dict[str, Any]:
    """Validate GitHub's release response and return its sole asset."""
    if release.get("tag_name") != tag:
        raise ReleaseVerificationError("release tag does not match the requested tag")
    if release.get("draft") is not False:
        raise ReleaseVerificationError("release is still a draft")
    if release.get("prerelease") is not False:
        raise ReleaseVerificationError("release is still a prerelease")
    if release.get("immutable") is not True:
        raise ReleaseVerificationError("release is not immutable")
    published_at = release.get("published_at")
    if (
        not isinstance(published_at, str)
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
            published_at,
        )
        is None
    ):
        raise ReleaseVerificationError("release has no valid publication timestamp")

    assets = release.get("assets")
    if not isinstance(assets, list) or len(assets) != 1:
        count = len(assets) if isinstance(assets, list) else 0
        raise ReleaseVerificationError(
            f"release must contain exactly one asset; GitHub reported {count}"
        )

    asset = assets[0]
    if not isinstance(asset, dict):
        raise ReleaseVerificationError("release asset metadata is not an object")
    if asset.get("name") != expected_asset_name:
        raise ReleaseVerificationError("release asset name does not match the expected ZIP")
    if asset.get("state") != "uploaded":
        raise ReleaseVerificationError("release asset is not in the uploaded state")
    if asset.get("content_type") != "application/zip":
        raise ReleaseVerificationError("release asset content type is not application/zip")

    digest = asset.get("digest")
    if not isinstance(digest, str) or SHA256_DIGEST.fullmatch(digest) is None:
        raise ReleaseVerificationError("release asset has no valid GitHub SHA-256 digest")
    if digest != expected_digest:
        raise ReleaseVerificationError(
            "GitHub release digest does not match the locally built asset"
        )

    expected_url = f"https://github.com/{repository}/releases/download/{tag}/{expected_asset_name}"
    if asset.get("browser_download_url") != expected_url:
        raise ReleaseVerificationError("release asset download URL does not match the release")

    return asset


def validate_tag_ref(tag_ref: dict[str, Any], tag: str, expected_commit: str) -> None:
    """Require the published tag to resolve directly to the workflow commit."""
    target = tag_ref.get("object")
    if (
        tag_ref.get("ref") != f"refs/tags/{tag}"
        or not isinstance(target, dict)
        or target.get("type") != "commit"
        or target.get("sha") != expected_commit
    ):
        raise ReleaseVerificationError("release tag ref does not name the expected commit")


def verify_asset_bytes(content: bytes, expected_digest: str) -> None:
    """Verify downloaded bytes against GitHub's digest and the ZIP structure."""
    actual_digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    if actual_digest != expected_digest:
        raise ReleaseVerificationError("downloaded asset digest does not match GitHub's SHA-256")

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            corrupt_member = archive.testzip()
    except zipfile.BadZipFile as error:
        raise ReleaseVerificationError("downloaded asset is not a valid ZIP archive") from error

    if corrupt_member is not None:
        raise ReleaseVerificationError("downloaded asset is not a valid ZIP archive")


def local_asset_digest(path: Path, expected_name: str) -> str:
    """Validate the locally built ZIP and return its SHA-256 identity."""
    if path.name != expected_name:
        raise ReleaseVerificationError("local asset name does not match the expected ZIP")
    if path.is_symlink() or not path.is_file():
        raise ReleaseVerificationError("local release asset is missing or unsafe")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ReleaseVerificationError("local release asset is unreadable") from error
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    verify_asset_bytes(content, digest)
    return digest


def _headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch_json(url: str, token: str | None, description: str) -> dict[str, Any]:
    response = requests.get(url, headers=_headers(token), timeout=30)
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise ReleaseVerificationError(
            f"{description} failed with HTTP {response.status_code}"
        ) from error

    try:
        payload = response.json()
    except requests.JSONDecodeError as error:
        raise ReleaseVerificationError(f"{description} returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise ReleaseVerificationError(f"{description} did not return an object")
    return payload


def verify_published_release(
    repository: str,
    tag: str,
    expected_asset_name: str,
    expected_digest: str,
    expected_commit: str,
    *,
    token: str | None = None,
    attempts: int = 6,
    interval_seconds: float = 5.0,
) -> str:
    """Poll until tag, release metadata, and public bytes satisfy one identity."""
    if attempts < 1:
        raise ValueError("attempts must be at least one")
    if SHA256_DIGEST.fullmatch(expected_digest) is None:
        raise ValueError("expected_digest must be a SHA-256 digest")
    if re.fullmatch(r"[0-9a-f]{40}", expected_commit) is None:
        raise ValueError("expected_commit must be a full Git commit SHA")

    last_error: ReleaseVerificationError | None = None
    for attempt in range(1, attempts + 1):
        try:
            tag_ref = _fetch_json(
                f"https://api.github.com/repos/{repository}/git/ref/tags/{tag}",
                token,
                "GitHub tag lookup",
            )
            validate_tag_ref(tag_ref, tag, expected_commit)
            release = _fetch_json(
                f"https://api.github.com/repos/{repository}/releases/tags/{tag}",
                token,
                "GitHub release lookup",
            )
            asset = validate_release_metadata(
                release,
                repository,
                tag,
                expected_asset_name,
                expected_digest,
            )
            download = requests.get(asset["browser_download_url"], timeout=120)
            try:
                download.raise_for_status()
            except requests.HTTPError as error:
                raise ReleaseVerificationError(
                    f"release asset download failed with HTTP {download.status_code}"
                ) from error
            verify_asset_bytes(download.content, expected_digest)
            return expected_digest
        except (ReleaseVerificationError, requests.RequestException) as error:
            last_error = (
                error
                if isinstance(error, ReleaseVerificationError)
                else ReleaseVerificationError("GitHub release lookup failed")
            )
            if attempt < attempts:
                time.sleep(interval_seconds)

    if last_error is None:
        raise ReleaseVerificationError("release verification exhausted without a result")
    raise last_error


def main() -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="GitHub owner/repository")
    parser.add_argument("--tag", required=True, help="Exact release tag")
    parser.add_argument("--expected-asset", required=True, help="Exact ZIP asset name")
    parser.add_argument("--local-asset", type=Path, required=True, help="Locally built ZIP")
    parser.add_argument("--expected-commit", required=True, help="Exact source commit SHA")
    parser.add_argument("--attempts", type=int, default=6)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    try:
        digest = local_asset_digest(args.local_asset, args.expected_asset)
        digest = verify_published_release(
            args.repository,
            args.tag,
            args.expected_asset,
            digest,
            args.expected_commit,
            token=token,
            attempts=args.attempts,
            interval_seconds=args.interval_seconds,
        )
    except (ReleaseVerificationError, ValueError) as error:
        print(f"Release verification failed: {error}", file=sys.stderr)
        return 1

    print(f"Release verification passed: immutable asset {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
