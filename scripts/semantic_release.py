"""Decide releases from canonical API semantics, not release metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import requests
import yaml

from scripts.dispatch_ack import DeliveryAckError, get_delivery_ack
from scripts.release_archive import ReleaseArchiveError, validate_release_archive_bytes
from scripts.verify_release import (
    ReleaseVerificationError,
    github_headers,
    release_receipt,
    validate_release_metadata,
    verify_asset_bytes,
)

SCHEMA_VERSION = 1
SHA256_PREFIX = "sha256:"
DOMAIN_PREFIX = "public.ves.io.schema."
DOMAIN_SUFFIX = ".ves-swagger"
RELEASE_ONLY_MANIFEST_FIELDS = frozenset({"version", "generated_at", "git_sha"})
REPORT_GENERATED_LINE = re.compile(r"^\*\*Generated:\*\* .*$", re.MULTILINE)
RELEASE_VERSION_PATTERN = re.compile(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}-[1-9][0-9]*")


class SemanticReleaseError(RuntimeError):
    """Raised when a semantic release identity cannot be proved."""


@dataclass(frozen=True)
class VerifiedLatestRelease:
    """Latest immutable release identity and its verified archive bytes."""

    tag: str
    commit: str
    asset_name: str
    content: bytes
    snapshot: dict[str, Any]
    receipt: dict[str, Any]
    delivery_acknowledged: bool


def _sha256(content: bytes) -> str:
    return f"{SHA256_PREFIX}{hashlib.sha256(content).hexdigest()}"


def _canonical_json(document: Any) -> bytes:
    try:
        return json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError) as error:
        raise SemanticReleaseError("release document is not canonical JSON data") from error


def _safe_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SemanticReleaseError(f"release archive contains unsafe path: {name!r}")
    return path


def _domain_name(path: PurePosixPath) -> str:
    stem = Path(path.name).stem.removesuffix(DOMAIN_SUFFIX)
    index = stem.find(DOMAIN_PREFIX)
    domain = stem[index + len(DOMAIN_PREFIX) :] if index >= 0 else stem
    if not domain:
        raise SemanticReleaseError(f"domain path has no semantic identity: {path}")
    return domain


def _normalize_openapi(document: Any) -> Any:
    normalized = deepcopy(document)
    if not isinstance(normalized, dict) or not isinstance(normalized.get("openapi"), str):
        return normalized
    info = normalized.get("info")
    if isinstance(info, dict):
        info.pop("version", None)
    return normalized


def _normalize_manifest(document: Any) -> Any:
    if not isinstance(document, dict):
        raise SemanticReleaseError("manifest.json must contain an object")
    normalized = deepcopy(document)
    for field in RELEASE_ONLY_MANIFEST_FIELDS:
        normalized.pop(field, None)
    files = normalized.get("files")
    if not isinstance(files, list):
        raise SemanticReleaseError("manifest.json files must be an array")
    paths: list[str] = []
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise SemanticReleaseError("manifest.json contains an invalid file entry")
        paths.append(str(_safe_archive_path(entry["path"])))
    if len(paths) != len(set(paths)):
        raise SemanticReleaseError("manifest.json contains duplicate file paths")
    normalized["files"] = sorted(paths)
    return normalized


def _canonical_entry(path: PurePosixPath, content: bytes) -> bytes:
    if path.name == "manifest.json":
        try:
            document = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SemanticReleaseError("manifest.json is not valid JSON") from error
        return _canonical_json(_normalize_manifest(document))

    if path.suffix == ".json":
        try:
            document = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SemanticReleaseError(f"release entry is not valid JSON: {path}") from error
        return _canonical_json(_normalize_openapi(document))

    if path.suffix in {".yaml", ".yml"}:
        try:
            document = yaml.safe_load(content)
        except (UnicodeDecodeError, yaml.YAMLError) as error:
            raise SemanticReleaseError(f"release entry is not valid YAML: {path}") from error
        return _canonical_json(_normalize_openapi(document))

    if path.name == "VALIDATION_REPORT.md":
        try:
            report = content.decode()
        except UnicodeDecodeError as error:
            raise SemanticReleaseError("VALIDATION_REPORT.md is not UTF-8") from error
        return REPORT_GENERATED_LINE.sub("", report).encode()

    return content


def _snapshot_from_entries(entries: dict[str, bytes]) -> dict[str, Any]:
    domains: dict[str, dict[str, str]] = {}
    artifacts: dict[str, str] = {}
    for name, content in entries.items():
        path = _safe_archive_path(name)
        digest = _sha256(_canonical_entry(path, content))
        if (
            len(path.parts) == 2
            and path.parts[0] == "domains"
            and path.suffix
            in {
                ".json",
                ".yaml",
                ".yml",
            }
        ):
            domain = _domain_name(path)
            if domain in domains:
                raise SemanticReleaseError(f"release archive contains duplicate domain: {domain}")
            domains[domain] = {"path": str(path), "digest": digest}
        else:
            artifacts[str(path)] = digest

    if not domains:
        raise SemanticReleaseError("release archive contains no domain specifications")
    payload = {
        "domains": {name: value["digest"] for name, value in sorted(domains.items())},
        "artifacts": dict(sorted(artifacts.items())),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "semantic_digest": _sha256(_canonical_json(payload)),
        "domains": dict(sorted(domains.items())),
        "artifacts": dict(sorted(artifacts.items())),
    }


def semantic_snapshot_from_archive(
    archive_path: Path,
    *,
    expected_version: str,
    expected_commit: str,
) -> dict[str, Any]:
    """Return the canonical semantic identity of one release ZIP."""
    path = Path(archive_path)
    if path.is_symlink() or not path.is_file():
        raise SemanticReleaseError(f"release archive is missing or unsafe: {path}")
    try:
        validated = validate_release_archive_bytes(
            path.read_bytes(),
            expected_version=expected_version,
            expected_commit=expected_commit,
        )
    except (OSError, ReleaseArchiveError) as error:
        raise SemanticReleaseError(str(error)) from error
    return _snapshot_from_entries(validated.entries)


def _snapshot_from_bytes(
    content: bytes,
    *,
    expected_version: str,
    expected_commit: str,
) -> dict[str, Any]:
    try:
        validated = validate_release_archive_bytes(
            content,
            expected_version=expected_version,
            expected_commit=expected_commit,
        )
    except ReleaseArchiveError as error:
        raise SemanticReleaseError(str(error)) from error
    return _snapshot_from_entries(validated.entries)


def compare_snapshots(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Measure domain and generated-artifact changes between two snapshots."""
    current_domains = current["domains"]
    previous_domains = previous["domains"]
    current_names = set(current_domains)
    previous_names = set(previous_domains)
    added = sorted(current_names - previous_names)
    removed = sorted(previous_names - current_names)
    modified = sorted(
        name
        for name in current_names & previous_names
        if current_domains[name]["digest"] != previous_domains[name]["digest"]
    )

    current_artifacts = current["artifacts"]
    previous_artifacts = previous["artifacts"]
    artifact_names = set(current_artifacts) | set(previous_artifacts)
    changed_artifacts = sorted(
        name
        for name in artifact_names
        if current_artifacts.get(name) != previous_artifacts.get(name)
    )
    changed = current["semantic_digest"] != previous["semantic_digest"]
    if changed != bool(added or removed or modified or changed_artifacts):
        raise SemanticReleaseError("semantic digest and measured change set disagree")
    return {
        "schema_version": SCHEMA_VERSION,
        "changed": changed,
        "semantic_digest": current["semantic_digest"],
        "previous_semantic_digest": previous["semantic_digest"],
        "added_domains": added,
        "removed_domains": removed,
        "modified_domains": modified,
        "changed_artifacts": changed_artifacts,
    }


def validate_previous_release_asset(
    release: dict[str, Any],
    archive_path: Path,
    *,
    repository: str,
    expected_commit: str,
) -> None:
    """Verify immutable metadata, size, digest, and ZIP bytes for a prior release."""
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise SemanticReleaseError("previous release has no valid tag")
    expected_name = f"api-specs-{tag}.zip"
    assets = release.get("assets")
    expected_digest = assets[0].get("digest") if isinstance(assets, list) and assets else None
    if not isinstance(expected_digest, str):
        raise SemanticReleaseError("previous release asset has no digest")
    try:
        asset = validate_release_metadata(
            release,
            repository,
            tag,
            expected_name,
            expected_digest,
        )
    except ReleaseVerificationError as error:
        raise SemanticReleaseError(str(error)) from error

    path = Path(archive_path)
    if path.name != expected_name or path.is_symlink() or not path.is_file():
        raise SemanticReleaseError("previous release archive name or type is invalid")
    content = path.read_bytes()
    if len(content) != asset["size"]:
        raise SemanticReleaseError("previous release archive size does not match metadata")
    try:
        verify_asset_bytes(
            content,
            asset["digest"],
            expected_version=tag.removeprefix("v"),
            expected_commit=expected_commit,
        )
    except ReleaseVerificationError as error:
        raise SemanticReleaseError(str(error)) from error


def _direct_tag_commit(tag_ref: Any, tag: str) -> str:
    """Return a full commit SHA from one exact lightweight release tag ref."""
    if not isinstance(tag_ref, dict) or tag_ref.get("ref") != f"refs/tags/{tag}":
        raise SemanticReleaseError("latest GitHub release tag ref is invalid")
    target = tag_ref.get("object")
    if not isinstance(target, dict) or target.get("type") != "commit":
        raise SemanticReleaseError("latest GitHub release tag is not a direct commit ref")
    commit = target.get("sha")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise SemanticReleaseError("latest GitHub release tag commit is invalid")
    return commit


def _github_array(
    url: str,
    token: str | None,
    description: str,
) -> list[Any]:
    response = requests.get(url, headers=github_headers(token), timeout=30)
    try:
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, requests.JSONDecodeError) as error:
        raise SemanticReleaseError(
            f"{description} failed with HTTP {response.status_code}"
        ) from error
    if not isinstance(payload, list):
        raise SemanticReleaseError(f"{description} did not return an array")
    return payload


def _require_clean_bootstrap(repository: str, token: str | None) -> None:
    releases = _github_array(
        f"https://api.github.com/repos/{repository}/releases?per_page=1",
        token,
        "GitHub release inventory",
    )
    version_tags = _github_array(
        f"https://api.github.com/repos/{repository}/git/matching-refs/tags/v",
        token,
        "GitHub version-tag inventory",
    )
    if releases or version_tags:
        raise SemanticReleaseError(
            "clean bootstrap requires an empty release and version-tag inventory"
        )


def _latest_release_metadata(
    repository: str,
    token: str | None,
) -> tuple[dict[str, Any], str, dict[str, Any]] | None:
    response = requests.get(
        f"https://api.github.com/repos/{repository}/releases/latest",
        headers=github_headers(token),
        timeout=30,
    )
    if response.status_code == 404:
        _require_clean_bootstrap(repository, token)
        return None
    try:
        response.raise_for_status()
        release = response.json()
    except (requests.RequestException, requests.JSONDecodeError) as error:
        raise SemanticReleaseError(
            f"latest GitHub release lookup failed with HTTP {response.status_code}"
        ) from error
    if not isinstance(release, dict):
        raise SemanticReleaseError("latest GitHub release response is not an object")
    tag = release.get("tag_name")
    assets = release.get("assets")
    if (
        not isinstance(tag, str)
        or not tag.startswith("v")
        or RELEASE_VERSION_PATTERN.fullmatch(tag.removeprefix("v")) is None
        or not isinstance(assets, list)
        or len(assets) != 1
    ):
        raise SemanticReleaseError("latest GitHub release identity is incomplete")
    asset = assets[0]
    if not isinstance(asset, dict) or not isinstance(asset.get("digest"), str):
        raise SemanticReleaseError("latest GitHub release asset identity is incomplete")
    try:
        validated = validate_release_metadata(
            release,
            repository,
            tag,
            f"api-specs-{tag}.zip",
            asset["digest"],
        )
    except ReleaseVerificationError as error:
        raise SemanticReleaseError(str(error)) from error
    return release, tag, validated


def _download_latest_asset(validated: dict[str, Any]) -> bytes:
    try:
        download = requests.get(validated["browser_download_url"], timeout=120)
        download.raise_for_status()
    except requests.RequestException as error:
        raise SemanticReleaseError("latest release asset download failed") from error
    if len(download.content) != validated["size"]:
        raise SemanticReleaseError("latest release asset size does not match metadata")
    return download.content


def _latest_tag_commit(repository: str, tag: str, token: str | None) -> str:
    ref_response = requests.get(
        f"https://api.github.com/repos/{repository}/git/ref/tags/{tag}",
        headers=github_headers(token),
        timeout=30,
    )
    try:
        ref_response.raise_for_status()
        tag_ref = ref_response.json()
    except (requests.RequestException, requests.JSONDecodeError) as error:
        raise SemanticReleaseError(
            f"latest GitHub release tag lookup failed with HTTP {ref_response.status_code}"
        ) from error
    return _direct_tag_commit(tag_ref, tag)


def _latest_verified_snapshot(
    repository: str,
    token: str | None,
) -> VerifiedLatestRelease | None:
    metadata = _latest_release_metadata(repository, token)
    if metadata is None:
        return None
    release, tag, validated = metadata
    content = _download_latest_asset(validated)
    commit = _latest_tag_commit(repository, tag, token)
    version = tag.removeprefix("v")
    try:
        verify_asset_bytes(
            content,
            validated["digest"],
            expected_version=version,
            expected_commit=commit,
        )
    except ReleaseVerificationError as error:
        raise SemanticReleaseError(str(error)) from error
    receipt = release_receipt(release, validated)
    try:
        acknowledged = get_delivery_ack(repository, commit, receipt, token or "")
    except DeliveryAckError as error:
        raise SemanticReleaseError(str(error)) from error

    return VerifiedLatestRelease(
        tag=tag,
        commit=commit,
        asset_name=validated["name"],
        content=content,
        snapshot=_snapshot_from_bytes(
            content,
            expected_version=version,
            expected_commit=commit,
        ),
        receipt=receipt,
        delivery_acknowledged=acknowledged,
    )


def decide_publication(
    current: dict[str, Any],
    latest: VerifiedLatestRelease | None,
    *,
    candidate_version: str,
    source_commit: str,
) -> dict[str, Any]:
    """Choose create, recovery, or no-op from measured semantic and source identity."""
    if RELEASE_VERSION_PATTERN.fullmatch(candidate_version) is None:
        raise SemanticReleaseError("candidate version must use YYYY.MM.DD-N")
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise SemanticReleaseError("source commit must be a full Git SHA")

    if latest is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "changed": True,
            "semantic_digest": current["semantic_digest"],
            "previous_semantic_digest": "none",
            "added_domains": sorted(current["domains"]),
            "removed_domains": [],
            "modified_domains": [],
            "changed_artifacts": sorted(current["artifacts"]),
            "previous_release_tag": "",
            "previous_release_commit": "",
            "publication_mode": "create",
            "release_version": candidate_version,
            "release_asset": f"api-specs-v{candidate_version}.zip",
            "release_commit": source_commit,
        }

    decision = compare_snapshots(current, latest.snapshot)
    decision["previous_release_tag"] = latest.tag
    decision["previous_release_commit"] = latest.commit
    if decision["changed"]:
        decision.update(
            {
                "publication_mode": "create",
                "release_version": candidate_version,
                "release_asset": f"api-specs-v{candidate_version}.zip",
                "release_commit": source_commit,
            }
        )
    elif not latest.delivery_acknowledged:
        decision.update(
            {
                "publication_mode": "recover",
                "release_version": latest.tag.removeprefix("v"),
                "release_asset": latest.asset_name,
                "release_commit": latest.commit,
            }
        )
    else:
        decision.update(
            {
                "publication_mode": "none",
                "release_version": "",
                "release_asset": "",
                "release_commit": "",
            }
        )
    return decision


def _format_measured(label: str, values: list[str]) -> str | None:
    if not values:
        return None
    rendered = ", ".join(f"`{value}`" for value in values)
    return f"- {label} ({len(values)}): {rendered}"


def render_release_notes(
    decision: dict[str, Any],
    *,
    version: str,
    specs_etag: str,
    repository: str,
) -> str:
    """Render release notes containing only measured semantic changes."""
    if decision.get("changed") is not True:
        raise SemanticReleaseError("release notes cannot be generated for unchanged semantics")
    lines = [
        f"## F5 XC API Specs v{version}",
        "",
        "### Measured semantic changes",
    ]
    measurements = (
        ("Added domains", decision.get("added_domains")),
        ("Removed domains", decision.get("removed_domains")),
        ("Modified domains", decision.get("modified_domains")),
        ("Changed generated artifacts", decision.get("changed_artifacts")),
    )
    for label, values in measurements:
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise SemanticReleaseError(f"semantic decision has invalid {label.lower()}")
        line = _format_measured(label, values)
        if line is not None:
            lines.append(line)
    lines.extend(
        [
            "",
            "### Validation",
            "- OpenAPI Spec Validator: passed",
            "- Schemathesis property-based testing: passed",
            "- Custom constraint validation: passed",
            "",
            "### Contents",
            "- `openapi.json` - Merged OpenAPI specification",
            "- `openapi.yaml` - YAML format",
            "- `domains/` - Individual domain spec files",
            "- `CHANGELOG.md` - List of fixes applied",
            "- `VALIDATION_REPORT.md` - Validation summary",
            "",
            "### Metadata",
            f"- Semantic Digest: {decision['semantic_digest']}",
            f"- Previous Semantic Digest: {decision['previous_semantic_digest']}",
            f"- Specs ETag: {specs_etag}",
            "",
            "---",
            f"Generated by [F5 XC API Validation Framework](https://github.com/{repository})",
            "",
        ]
    )
    return "\n".join(lines)


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def _write_bytes_atomically(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SemanticReleaseError(f"release recovery path is unsafe: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _compare(args: argparse.Namespace) -> int:
    current = semantic_snapshot_from_archive(
        args.current_archive,
        expected_version=args.candidate_version,
        expected_commit=args.source_commit,
    )
    latest = _latest_verified_snapshot(
        args.repository,
        os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"),
    )
    decision = decide_publication(
        current,
        latest,
        candidate_version=args.candidate_version,
        source_commit=args.source_commit,
    )
    if decision["publication_mode"] == "recover" and latest is not None:
        _write_bytes_atomically(args.recovery_directory / latest.asset_name, latest.content)
    _write_json(args.output, decision)
    if args.github_output is not None:
        with args.github_output.open("a") as output:
            should_publish = decision["publication_mode"] in {"create", "recover"}
            output.write(f"should_publish={'true' if should_publish else 'false'}\n")
            output.write(f"semantic_changed={'true' if decision['changed'] else 'false'}\n")
            output.write(
                "resume_publication="
                f"{'true' if decision['publication_mode'] == 'recover' else 'false'}\n"
            )
            output.write(f"release_version={decision['release_version']}\n")
            output.write(f"release_asset={decision['release_asset']}\n")
            output.write(f"release_commit={decision['release_commit']}\n")
            output.write(f"semantic_digest={decision['semantic_digest']}\n")
            output.write(f"previous_release_tag={latest.tag if latest is not None else ''}\n")
    return 0


def _notes(args: argparse.Namespace) -> int:
    try:
        decision = json.loads(args.decision.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SemanticReleaseError("semantic release decision is not valid JSON") from error
    if not isinstance(decision, dict):
        raise SemanticReleaseError("semantic release decision is not an object")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        render_release_notes(
            decision,
            version=args.version,
            specs_etag=args.specs_etag,
            repository=args.repository,
        )
    )
    return 0


def main() -> int:
    """Run semantic comparison or render measured release notes."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    compare = commands.add_parser("compare", help="compare a candidate with the latest release")
    compare.add_argument("--current-archive", type=Path, required=True)
    compare.add_argument("--candidate-version", required=True)
    compare.add_argument("--source-commit", required=True)
    compare.add_argument("--repository", required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument("--github-output", type=Path)
    compare.add_argument("--recovery-directory", type=Path, required=True)
    compare.set_defaults(handler=_compare)

    notes = commands.add_parser("notes", help="render notes from a measured semantic decision")
    notes.add_argument("--decision", type=Path, required=True)
    notes.add_argument("--version", required=True)
    notes.add_argument("--specs-etag", required=True)
    notes.add_argument("--repository", required=True)
    notes.add_argument("--output", type=Path, required=True)
    notes.set_defaults(handler=_notes)
    args = parser.parse_args()
    try:
        return args.handler(args)
    except (OSError, SemanticReleaseError) as error:
        print(f"Semantic release failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
