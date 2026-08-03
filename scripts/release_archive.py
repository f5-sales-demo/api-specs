"""Strict validation for one immutable api-specs release archive."""

from __future__ import annotations

import hashlib
import io
import re
import stat
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from scripts.utils.strict_data import (
    StrictDataError,
    canonical_posix_path,
    strict_json_loads,
    strict_yaml_loads,
)

MANIFEST_FIELDS = frozenset({"schema_version", "version", "generated_at", "git_sha", "files"})
FILE_FIELDS = frozenset({"path", "size", "sha256"})
VERSION_PATTERN = re.compile(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}-[1-9][0-9]*")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
MAX_ARCHIVE_FILES = 1_000
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
CANONICAL_FILE_MODE = 0o100644
END_OF_CENTRAL_DIRECTORY_SIZE = 22


class ReleaseArchiveError(RuntimeError):
    """Raised when release bytes cannot prove their manifest and provenance."""


@dataclass(frozen=True)
class ValidatedReleaseArchive:
    """Validated manifest plus exact uncompressed member bytes."""

    manifest: dict[str, Any]
    entries: dict[str, bytes]


def _load_json(content: bytes, path: str) -> Any:
    try:
        return strict_json_loads(content, path)
    except StrictDataError as error:
        raise ReleaseArchiveError(str(error)) from error


def _load_yaml(content: bytes, path: str) -> Any:
    try:
        return strict_yaml_loads(content, path)
    except StrictDataError as error:
        raise ReleaseArchiveError(str(error)) from error


def _canonical_path(name: str) -> str:
    try:
        return canonical_posix_path(name).as_posix()
    except StrictDataError as error:
        raise ReleaseArchiveError(f"release ZIP does not use a canonical path: {name!r}") from error


def _validate_version(value: Any, label: str) -> str:
    if not isinstance(value, str) or VERSION_PATTERN.fullmatch(value) is None:
        raise ReleaseArchiveError(f"{label} is not a canonical release version")
    try:
        datetime.strptime(value.rsplit("-", 1)[0], "%Y.%m.%d")
    except ValueError as error:
        raise ReleaseArchiveError(f"{label} contains an invalid date") from error
    return value


def _validate_generated_at(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ReleaseArchiveError("manifest generated_at is not a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseArchiveError("manifest generated_at is not a timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReleaseArchiveError("manifest generated_at requires a UTC offset")
    return parsed


def _read_entries(content: bytes) -> tuple[dict[str, bytes], dict[str, zipfile.ZipInfo]]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if archive.comment:
                raise ReleaseArchiveError("release ZIP contains an archive comment")
            if (
                not content.startswith(b"PK\x03\x04")
                or len(content) < END_OF_CENTRAL_DIRECTORY_SIZE
                or content[-END_OF_CENTRAL_DIRECTORY_SIZE:-18] != b"PK\x05\x06"
                or content[-2:] != b"\x00\x00"
            ):
                raise ReleaseArchiveError("release ZIP has bytes outside its canonical envelope")
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ARCHIVE_FILES:
                raise ReleaseArchiveError("release ZIP member count is outside the safe limit")
            if min(info.header_offset for info in infos) != 0:
                raise ReleaseArchiveError("release ZIP has bytes outside its canonical envelope")
            if sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
                raise ReleaseArchiveError("release ZIP expands beyond the safe size limit")
            entries: dict[str, bytes] = {}
            metadata: dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                name = _canonical_path(info.filename)
                _validate_member_metadata(info, name)
                if name in entries:
                    raise ReleaseArchiveError(f"release ZIP contains duplicate path: {name}")
                entries[name] = archive.read(info)
                metadata[name] = info
            if list(entries) != sorted(entries):
                raise ReleaseArchiveError("release ZIP members are not in canonical path order")
            corrupt = archive.testzip()
            if corrupt is not None:
                raise ReleaseArchiveError(f"release ZIP has a corrupt member: {corrupt}")
            return entries, metadata
    except zipfile.BadZipFile as error:
        raise ReleaseArchiveError("release asset is not a valid ZIP archive") from error


def _validate_member_metadata(info: zipfile.ZipInfo, name: str) -> None:
    if info.is_dir():
        raise ReleaseArchiveError(f"release ZIP contains a directory entry: {name}")
    if info.flag_bits & 0x1:
        raise ReleaseArchiveError(f"release ZIP member is encrypted: {name}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ReleaseArchiveError(f"release ZIP member is a symbolic link: {name}")
    if info.compress_type != zipfile.ZIP_STORED:
        raise ReleaseArchiveError(f"release ZIP member is not stored: {name}")
    if info.create_system != 3:
        raise ReleaseArchiveError(f"release ZIP member has noncanonical create_system: {name}")
    if info.external_attr != CANONICAL_FILE_MODE << 16:
        raise ReleaseArchiveError(f"release ZIP member has noncanonical mode: {name}")
    if info.internal_attr != 0:
        raise ReleaseArchiveError(
            f"release ZIP member has noncanonical internal attributes: {name}"
        )
    if info.extra:
        raise ReleaseArchiveError(f"release ZIP member has noncanonical extra data: {name}")
    if info.comment:
        raise ReleaseArchiveError(f"release ZIP member has a comment: {name}")


def _validate_manifest(
    manifest: Any,
    entries: dict[str, bytes],
    expected_version: str,
    expected_commit: str,
) -> dict[str, Any]:
    validated = _validate_manifest_identity(manifest, expected_version, expected_commit)
    manifest = validated
    files = manifest["files"]
    if not isinstance(files, list):
        raise ReleaseArchiveError("manifest files must be an array")

    measured: dict[str, tuple[int, str]] = {}
    listed_paths: list[str] = []
    for index, entry in enumerate(files):
        path, size, digest = _validated_file_entry(index, entry)
        if path in measured:
            raise ReleaseArchiveError(f"manifest contains duplicate file path: {path}")
        measured[path] = (size, digest)
        listed_paths.append(path)
    actual_paths = set(entries) - {"manifest.json"}
    if set(measured) != actual_paths:
        raise ReleaseArchiveError("manifest does not name the exact ZIP members")
    if listed_paths != sorted(listed_paths):
        raise ReleaseArchiveError("manifest files are not in canonical path order")
    for path, (size, digest) in measured.items():
        actual = entries[path]
        if len(actual) != size:
            raise ReleaseArchiveError(f"manifest size does not match ZIP member: {path}")
        if hashlib.sha256(actual).hexdigest() != digest:
            raise ReleaseArchiveError(f"manifest digest does not match ZIP member: {path}")
    return manifest


def _validate_manifest_identity(
    manifest: Any,
    expected_version: str,
    expected_commit: str,
) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ReleaseArchiveError("manifest.json must contain an object")
    if set(manifest) != MANIFEST_FIELDS:
        raise ReleaseArchiveError("manifest.json must contain exact fields")
    if manifest["schema_version"] != 1 or isinstance(manifest["schema_version"], bool):
        raise ReleaseArchiveError("manifest schema_version must be 1")
    version = _validate_version(manifest["version"], "manifest version")
    if version != expected_version:
        raise ReleaseArchiveError("manifest version does not match the expected version")
    _validate_generated_at(manifest["generated_at"])
    commit = manifest["git_sha"]
    if not isinstance(commit, str) or COMMIT_PATTERN.fullmatch(commit) is None:
        raise ReleaseArchiveError("manifest commit is not a full Git SHA")
    if commit != expected_commit:
        raise ReleaseArchiveError("manifest commit does not match the expected commit")
    return manifest


def _validated_file_entry(index: int, entry: Any) -> tuple[str, int, str]:
    if not isinstance(entry, dict) or set(entry) != FILE_FIELDS:
        raise ReleaseArchiveError(f"manifest files[{index}] must contain exact fields")
    path = entry["path"]
    if not isinstance(path, str):
        raise ReleaseArchiveError(f"manifest files[{index}] path is invalid")
    canonical_path = _canonical_path(path)
    size = entry["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ReleaseArchiveError(f"manifest files[{index}] size is invalid")
    digest = entry["sha256"]
    if not isinstance(digest, str) or DIGEST_PATTERN.fullmatch(digest) is None:
        raise ReleaseArchiveError(f"manifest files[{index}] digest is invalid")
    return canonical_path, size, digest


def _validate_release_shape(
    manifest: dict[str, Any],
    entries: dict[str, bytes],
    expected_version: str,
) -> None:
    required = {
        "openapi.json",
        "openapi.yaml",
        "CHANGELOG.md",
        "VALIDATION_REPORT.md",
        "manifest.json",
    }
    missing = required - set(entries)
    if missing:
        raise ReleaseArchiveError(
            f"release ZIP is missing required members: {', '.join(sorted(missing))}"
        )
    domain_paths = [
        path
        for path in entries
        if path.startswith("domains/") and PurePosixPath(path).suffix in {".json", ".yaml", ".yml"}
    ]
    if not domain_paths:
        raise ReleaseArchiveError("release ZIP contains no domain specifications")

    parsed_documents: dict[str, Any] = {}
    for path, member in entries.items():
        suffix = PurePosixPath(path).suffix
        if suffix == ".json":
            parsed_documents[path] = _load_json(member, path)
        elif suffix in {".yaml", ".yml"}:
            parsed_documents[path] = _load_yaml(member, path)

    for path in domain_paths:
        _validate_openapi_document(parsed_documents[path], path)

    aggregate_json = parsed_documents["openapi.json"]
    aggregate_yaml = parsed_documents["openapi.yaml"]
    if aggregate_json != aggregate_yaml:
        raise ReleaseArchiveError("aggregate JSON and YAML documents do not match")
    _validate_openapi_document(aggregate_json, "aggregate")
    info = aggregate_json.get("info")
    if not isinstance(info, dict) or info.get("version") != expected_version:
        raise ReleaseArchiveError("aggregate OpenAPI version does not match release version")
    generated_line = f"**Generated:** {manifest['generated_at']}"
    try:
        report = entries["VALIDATION_REPORT.md"].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReleaseArchiveError("VALIDATION_REPORT.md is not UTF-8") from error
    if generated_line not in report.splitlines():
        raise ReleaseArchiveError("validation report does not match manifest generated_at")


def _validate_openapi_document(document: Any, path: str) -> None:
    if not isinstance(document, dict):
        raise ReleaseArchiveError(f"{path} is not an OpenAPI object")
    version = document.get("openapi")
    if not isinstance(version, str) or not version:
        raise ReleaseArchiveError(f"{path} OpenAPI version is invalid")
    if not isinstance(document.get("paths"), dict):
        raise ReleaseArchiveError(f"{path} OpenAPI paths are not an object")


def _validate_zip_timestamps(
    metadata: dict[str, zipfile.ZipInfo],
    generated_at: Any,
) -> None:
    timestamp = _validate_generated_at(generated_at).astimezone(UTC)
    expected = (
        timestamp.year,
        timestamp.month,
        timestamp.day,
        timestamp.hour,
        timestamp.minute,
        timestamp.second - timestamp.second % 2,
    )
    for path, info in metadata.items():
        if info.date_time != expected:
            raise ReleaseArchiveError(f"release ZIP member timestamp is noncanonical: {path}")


def validate_release_archive_bytes(
    content: bytes,
    *,
    expected_version: str,
    expected_commit: str,
) -> ValidatedReleaseArchive:
    """Prove archive structure, manifest truth, and external provenance."""
    _validate_version(expected_version, "expected version")
    if not isinstance(expected_commit, str) or COMMIT_PATTERN.fullmatch(expected_commit) is None:
        raise ReleaseArchiveError("expected commit is not a full Git SHA")
    entries, metadata = _read_entries(content)
    if "manifest.json" not in entries:
        raise ReleaseArchiveError("release ZIP contains no root manifest.json")
    manifest = _load_json(entries["manifest.json"], "manifest.json")
    validated_manifest = _validate_manifest(
        manifest,
        entries,
        expected_version,
        expected_commit,
    )
    _validate_zip_timestamps(metadata, validated_manifest["generated_at"])
    _validate_release_shape(validated_manifest, entries, expected_version)
    return ValidatedReleaseArchive(manifest=validated_manifest, entries=entries)
