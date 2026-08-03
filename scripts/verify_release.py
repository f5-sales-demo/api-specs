"""Verify that a published GitHub release is sealed and byte-identical.

The downstream enrichment pipeline treats an API specification release as an
immutable input.  This verifier is deliberately fail-closed: the producer
workflow does not dispatch downstream until GitHub reports one final,
immutable release asset and the downloaded ZIP matches GitHub's SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from scripts.install_release import (
    ReleaseInstallError,
    install_release_archive,
    write_new_file_atomically,
)
from scripts.release_archive import ReleaseArchiveError, validate_release_archive_bytes

GITHUB_API_VERSION = "2022-11-28"
SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class ReleaseVerificationError(RuntimeError):
    """Raised when a published release does not satisfy the release contract."""


@dataclass(frozen=True)
class ReleaseVerificationRetry:
    """Bounded retry policy for one complete publication verification."""

    attempts: int = 6
    interval_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least one")
        if self.interval_seconds < 0:
            raise ValueError("interval_seconds cannot be negative")


@dataclass(frozen=True)
class ExpectedRelease:
    """One immutable publication identity expected from GitHub."""

    repository: str
    tag: str
    asset_name: str
    asset_digest: str
    commit: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None:
            raise ValueError("repository must be an owner/name pair")
        if not self.tag.startswith("v") or len(self.tag) == 1:
            raise ValueError("tag must be a v-prefixed release tag")
        if self.asset_name != f"api-specs-{self.tag}.zip":
            raise ValueError("asset_name must match the release tag")
        if SHA256_DIGEST.fullmatch(self.asset_digest) is None:
            raise ValueError("asset_digest must be a SHA-256 digest")
        if re.fullmatch(r"[0-9a-f]{40}", self.commit) is None:
            raise ValueError("commit must be a full Git commit SHA")


@dataclass(frozen=True)
class VerifiedReleaseDownload:
    """Exact public release metadata and downloaded bytes from one successful poll."""

    release: dict[str, Any]
    asset: dict[str, Any]
    content: bytes


def _validate_release_state(release: dict[str, Any], tag: str) -> None:
    """Require one final immutable release at the expected tag."""
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


def _sole_release_asset(release: dict[str, Any]) -> dict[str, Any]:
    """Return the release's only asset."""
    assets = release.get("assets")
    if not isinstance(assets, list) or len(assets) != 1:
        count = len(assets) if isinstance(assets, list) else 0
        raise ReleaseVerificationError(
            f"release must contain exactly one asset; GitHub reported {count}"
        )

    asset = assets[0]
    if not isinstance(asset, dict):
        raise ReleaseVerificationError("release asset metadata is not an object")
    return asset


def _validate_release_asset(
    asset: dict[str, Any],
    expected_asset_name: str,
    expected_digest: str,
    expected_url: str,
) -> None:
    """Require exact uploaded ZIP metadata for one release asset."""
    if asset.get("name") != expected_asset_name:
        raise ReleaseVerificationError("release asset name does not match the expected ZIP")
    if asset.get("state") != "uploaded":
        raise ReleaseVerificationError("release asset is not in the uploaded state")
    if asset.get("content_type") != "application/zip":
        raise ReleaseVerificationError("release asset content type is not application/zip")
    size = asset.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ReleaseVerificationError("release asset size is not a positive integer")

    digest = asset.get("digest")
    if not isinstance(digest, str) or SHA256_DIGEST.fullmatch(digest) is None:
        raise ReleaseVerificationError("release asset has no valid GitHub SHA-256 digest")
    if digest != expected_digest:
        raise ReleaseVerificationError(
            "GitHub release digest does not match the locally built asset"
        )

    if asset.get("browser_download_url") != expected_url:
        raise ReleaseVerificationError("release asset download URL does not match the release")


def validate_release_metadata(
    release: dict[str, Any],
    repository: str,
    tag: str,
    expected_asset_name: str,
    expected_digest: str,
) -> dict[str, Any]:
    """Validate GitHub's release response and return its sole asset."""
    _validate_release_state(release, tag)
    asset = _sole_release_asset(release)
    expected_url = f"https://github.com/{repository}/releases/download/{tag}/{expected_asset_name}"
    _validate_release_asset(asset, expected_asset_name, expected_digest, expected_url)

    return asset


def release_receipt(release: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
    """Return the exact six-field immutable identity consumed downstream."""
    tag = release["tag_name"]
    if not isinstance(tag, str) or not tag.startswith("v") or len(tag) == 1:
        raise ReleaseVerificationError("release tag cannot form a downstream version")
    return {
        "version": tag.removeprefix("v"),
        "tag_name": tag,
        "published_at": release["published_at"],
        "asset_name": asset["name"],
        "asset_size": asset["size"],
        "asset_digest": asset["digest"],
    }


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


def verify_asset_bytes(
    content: bytes,
    expected_digest: str,
    *,
    expected_version: str,
    expected_commit: str,
) -> None:
    """Verify downloaded bytes against GitHub's digest and strict archive contract."""
    actual_digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    if actual_digest != expected_digest:
        raise ReleaseVerificationError("downloaded asset digest does not match GitHub's SHA-256")

    try:
        validate_release_archive_bytes(
            content,
            expected_version=expected_version,
            expected_commit=expected_commit,
        )
    except ReleaseArchiveError as error:
        raise ReleaseVerificationError(str(error)) from error


def local_asset_digest(
    path: Path,
    expected_name: str,
    *,
    expected_version: str,
    expected_commit: str,
) -> str:
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
    verify_asset_bytes(
        content,
        digest,
        expected_version=expected_version,
        expected_commit=expected_commit,
    )
    return digest


def github_headers(token: str | None = None) -> dict[str, str]:
    """Return pinned-version GitHub API headers with optional authentication."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch_json(url: str, token: str | None, description: str) -> dict[str, Any]:
    response = requests.get(url, headers=github_headers(token), timeout=30)
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


def _verify_public_release_once(
    expected: ExpectedRelease,
    token: str | None,
) -> VerifiedReleaseDownload:
    tag_ref = _fetch_json(
        f"https://api.github.com/repos/{expected.repository}/git/ref/tags/{expected.tag}",
        token,
        "GitHub tag lookup",
    )
    validate_tag_ref(tag_ref, expected.tag, expected.commit)
    release = _fetch_json(
        f"https://api.github.com/repos/{expected.repository}/releases/tags/{expected.tag}",
        token,
        "GitHub release lookup",
    )
    asset = validate_release_metadata(
        release,
        expected.repository,
        expected.tag,
        expected.asset_name,
        expected.asset_digest,
    )
    download = requests.get(asset["browser_download_url"], timeout=120)
    try:
        download.raise_for_status()
    except requests.HTTPError as error:
        raise ReleaseVerificationError(
            f"release asset download failed with HTTP {download.status_code}"
        ) from error
    if len(download.content) != asset["size"]:
        raise ReleaseVerificationError(
            "downloaded asset size does not match GitHub release metadata"
        )
    verify_asset_bytes(
        download.content,
        expected.asset_digest,
        expected_version=expected.tag.removeprefix("v"),
        expected_commit=expected.commit,
    )
    return VerifiedReleaseDownload(release=release, asset=asset, content=download.content)


def verify_published_release(
    expected: ExpectedRelease,
    *,
    install_dir: Path,
    token: str | None = None,
    retry: ReleaseVerificationRetry | None = None,
    receipt_output: Path | None = None,
) -> str:
    """Poll until tag, release metadata, and public bytes satisfy one identity."""
    retry_policy = retry or ReleaseVerificationRetry()

    last_error: ReleaseVerificationError | None = None
    verified: VerifiedReleaseDownload | None = None
    for attempt in range(1, retry_policy.attempts + 1):
        try:
            verified = _verify_public_release_once(expected, token)
            break
        except (ReleaseVerificationError, requests.RequestException) as error:
            last_error = (
                error
                if isinstance(error, ReleaseVerificationError)
                else ReleaseVerificationError("GitHub release lookup failed")
            )
            if attempt < retry_policy.attempts:
                time.sleep(retry_policy.interval_seconds)

    if verified is None:
        if last_error is None:
            raise ReleaseVerificationError("release verification exhausted without a result")
        raise last_error

    try:
        install_release_archive(
            verified.content,
            install_dir,
            expected_version=expected.tag.removeprefix("v"),
            expected_commit=expected.commit,
        )
    except ReleaseInstallError as error:
        raise ReleaseVerificationError(str(error)) from error
    if receipt_output is not None:
        try:
            write_new_file_atomically(
                (
                    json.dumps(release_receipt(verified.release, verified.asset), indent=2) + "\n"
                ).encode(),
                receipt_output,
            )
        except ReleaseInstallError as error:
            raise ReleaseVerificationError(
                f"verified release receipt could not be written: {error}"
            ) from error
    return expected.asset_digest


def main() -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="GitHub owner/repository")
    parser.add_argument("--tag", required=True, help="Exact release tag")
    parser.add_argument("--expected-asset", required=True, help="Exact ZIP asset name")
    parser.add_argument("--local-asset", type=Path, required=True, help="Locally built ZIP")
    parser.add_argument("--expected-commit", required=True, help="Exact source commit SHA")
    parser.add_argument(
        "--receipt-output",
        type=Path,
        help="Write the verified six-field downstream receipt to this path",
    )
    parser.add_argument(
        "--install-dir",
        type=Path,
        required=True,
        help="Install the verified public release into this new local directory",
    )
    parser.add_argument("--attempts", type=int, default=6)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    try:
        digest = local_asset_digest(
            args.local_asset,
            args.expected_asset,
            expected_version=args.tag.removeprefix("v"),
            expected_commit=args.expected_commit,
        )
        digest = verify_published_release(
            ExpectedRelease(
                repository=args.repository,
                tag=args.tag,
                asset_name=args.expected_asset,
                asset_digest=digest,
                commit=args.expected_commit,
            ),
            install_dir=args.install_dir,
            token=token,
            retry=ReleaseVerificationRetry(
                attempts=args.attempts,
                interval_seconds=args.interval_seconds,
            ),
            receipt_output=args.receipt_output,
        )
    except (ReleaseVerificationError, ValueError) as error:
        print(f"Release verification failed: {error}", file=sys.stderr)
        return 1

    print(f"Release verification passed: immutable asset {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
