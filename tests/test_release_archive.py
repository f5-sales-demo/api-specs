"""Strict release ZIP manifest and provenance contracts."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Callable

import pytest

from scripts.install_release import ReleaseInstallError, install_release_archive
from scripts.release_archive import ReleaseArchiveError, validate_release_archive_bytes

VERSION = "2026.08.02-1"
COMMIT = "a" * 40
GENERATED_AT = "2026-08-02T07:08:10+00:00"


def _content() -> dict[str, bytes]:
    aggregate = json.dumps(
        {
            "openapi": "3.0.0",
            "info": {"title": "Aggregate", "version": VERSION},
            "paths": {},
            "components": {"schemas": {}},
        },
        indent=2,
    ).encode()
    return {
        "domains/widgets.json": json.dumps(
            {
                "openapi": "3.0.0",
                "info": {"title": "Widgets", "version": "2026.08.02"},
                "paths": {},
            },
            indent=2,
        ).encode(),
        "openapi.json": aggregate,
        "openapi.yaml": aggregate,
        "CHANGELOG.md": b"# Changelog\n",
        "VALIDATION_REPORT.md": (f"# Validation\n\n**Generated:** {GENERATED_AT}\n").encode(),
    }


def _archive(
    *,
    mutate_manifest: Callable[[dict], None] | None = None,
    mutate_files: Callable[[dict[str, bytes]], None] | None = None,
    raw_manifest: bytes | None = None,
    compression: int = zipfile.ZIP_STORED,
    mutate_member: Callable[[zipfile.ZipInfo], None] | None = None,
    archive_comment: bytes = b"",
    reverse_order: bool = False,
) -> bytes:
    files = _content()
    if mutate_files is not None:
        mutate_files(files)
    manifest = {
        "schema_version": 1,
        "version": VERSION,
        "generated_at": GENERATED_AT,
        "git_sha": COMMIT,
        "files": [
            {
                "path": path,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for path, content in sorted(files.items())
        ],
    }
    if mutate_manifest is not None:
        mutate_manifest(manifest)

    payloads = {
        **files,
        "manifest.json": (
            raw_manifest if raw_manifest is not None else json.dumps(manifest).encode()
        ),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for path in sorted(payloads, reverse=reverse_order):
            member = _canonical_member(path, compression)
            if mutate_member is not None:
                mutate_member(member)
            archive.writestr(member, payloads[path], compress_type=member.compress_type)
        archive.comment = archive_comment
    return output.getvalue()


def _canonical_member(
    path: str,
    compression: int = zipfile.ZIP_STORED,
) -> zipfile.ZipInfo:
    member = zipfile.ZipInfo(path, date_time=(2026, 8, 2, 7, 8, 10))
    member.compress_type = compression
    member.create_system = 3
    member.external_attr = 0o100644 << 16
    member.internal_attr = 0
    member.extra = b""
    member.comment = b""
    return member


def _validate(content: bytes) -> None:
    validate_release_archive_bytes(
        content,
        expected_version=VERSION,
        expected_commit=COMMIT,
    )


def test_valid_manifest_proves_every_member_size_digest_and_provenance() -> None:
    validated = validate_release_archive_bytes(
        _archive(),
        expected_version=VERSION,
        expected_commit=COMMIT,
    )

    assert validated.manifest["version"] == VERSION
    assert set(validated.entries) == {*_content(), "manifest.json"}


def test_verified_archive_installs_exact_bytes_atomically(tmp_path) -> None:
    archive = _archive()
    target = tmp_path / "installed"

    installation = install_release_archive(
        archive,
        target,
        expected_version=VERSION,
        expected_commit=COMMIT,
    )

    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        expected = {name: source.read(name) for name in source.namelist()}
    installed = {
        path.relative_to(target).as_posix(): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    assert installed == expected
    assert installation.target == target
    assert installation.file_count == len(expected)
    assert installation.installed_bytes == sum(map(len, expected.values()))
    assert all(
        (path.stat().st_mode & 0o777) == 0o644 for path in target.rglob("*") if path.is_file()
    )
    assert (target.stat().st_mode & 0o777) == 0o755
    assert all(
        (path.stat().st_mode & 0o777) == 0o755 for path in target.rglob("*") if path.is_dir()
    )


def test_release_install_never_overwrites_an_existing_target(tmp_path) -> None:
    target = tmp_path / "installed"
    target.mkdir()
    marker = target / "owner-data"
    marker.write_text("preserve\n", encoding="utf-8")

    with pytest.raises(ReleaseInstallError, match="already exists"):
        install_release_archive(
            _archive(),
            target,
            expected_version=VERSION,
            expected_commit=COMMIT,
        )

    assert marker.read_text(encoding="utf-8") == "preserve\n"


def test_invalid_release_leaves_no_target_or_partial_install(tmp_path) -> None:
    target = tmp_path / "installed"

    with pytest.raises(ReleaseInstallError, match="manifest digest"):
        install_release_archive(
            _archive(mutate_manifest=lambda manifest: manifest["files"][0].update(sha256="0" * 64)),
            target,
            expected_version=VERSION,
            expected_commit=COMMIT,
        )

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_release_zip_must_be_stored_and_in_canonical_member_order() -> None:
    with pytest.raises(ReleaseArchiveError, match="stored"):
        _validate(_archive(compression=zipfile.ZIP_DEFLATED))

    with pytest.raises(ReleaseArchiveError, match="order"):
        _validate(_archive(reverse_order=True))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda member: setattr(member, "date_time", (2026, 8, 2, 7, 8, 12)), "timestamp"),
        (lambda member: setattr(member, "create_system", 0), "create_system"),
        (lambda member: setattr(member, "external_attr", 0o100600 << 16), "mode"),
        (lambda member: setattr(member, "extra", b"\x01\x00\x00\x00"), "extra"),
        (lambda member: setattr(member, "comment", b"member comment"), "comment"),
    ],
)
def test_release_zip_member_metadata_is_canonical(
    mutation: Callable[[zipfile.ZipInfo], None],
    message: str,
) -> None:
    with pytest.raises(ReleaseArchiveError, match=message):
        _validate(_archive(mutate_member=mutation))


def test_release_zip_archive_comment_is_forbidden() -> None:
    with pytest.raises(ReleaseArchiveError, match="archive comment"):
        _validate(_archive(archive_comment=b"comment"))


@pytest.mark.parametrize(
    "tamper",
    [
        lambda content: b"prefix" + content,
        lambda content: content + b"suffix",
    ],
)
def test_release_zip_rejects_bytes_outside_the_canonical_envelope(
    tamper: Callable[[bytes], bytes],
) -> None:
    with pytest.raises(ReleaseArchiveError, match="envelope"):
        _validate(tamper(_archive()))


def test_every_domain_and_aggregate_must_be_an_openapi_object() -> None:
    with pytest.raises(ReleaseArchiveError, match="domains/widgets.json.*OpenAPI"):
        _validate(
            _archive(mutate_files=lambda files: files.update({"domains/widgets.json": b"[]"}))
        )

    aggregate = json.dumps({"info": {"version": VERSION}}).encode()
    with pytest.raises(ReleaseArchiveError, match="aggregate.*OpenAPI"):
        _validate(
            _archive(
                mutate_files=lambda files: files.update(
                    {"openapi.json": aggregate, "openapi.yaml": aggregate}
                )
            )
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda manifest: manifest["files"].pop(), "exact ZIP members"),
        (
            lambda manifest: manifest["files"].append(
                {"path": "missing.txt", "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}
            ),
            "exact ZIP members",
        ),
        (lambda manifest: manifest["files"][0].update(size=True), "size"),
        (lambda manifest: manifest["files"][0].update(size=-1), "size"),
        (lambda manifest: manifest["files"][0].update(size=999), "size"),
        (lambda manifest: manifest["files"][0].update(sha256="0" * 64), "digest"),
        (lambda manifest: manifest.update(version="2026.08.02-2"), "version"),
        (lambda manifest: manifest.update(git_sha="b" * 40), "commit"),
        (lambda manifest: manifest.update(generated_at="not-a-time"), "generated_at"),
        (lambda manifest: manifest.update(extra=True), "exact fields"),
    ],
)
def test_manifest_lies_fail_closed(mutation: Callable[[dict], None], message: str) -> None:
    with pytest.raises(ReleaseArchiveError, match=message):
        _validate(_archive(mutate_manifest=mutation))


def test_missing_manifest_fails_closed() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, content in sorted(_content().items()):
            archive.writestr(_canonical_member(path), content)

    with pytest.raises(ReleaseArchiveError, match="manifest.json"):
        _validate(output.getvalue())


def test_zip_symlink_member_fails_closed() -> None:
    files = _content()
    files["domains/link.json"] = b"widgets.json"
    manifest = {
        "schema_version": 1,
        "version": VERSION,
        "generated_at": GENERATED_AT,
        "git_sha": COMMIT,
        "files": [
            {
                "path": path,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for path, content in sorted(files.items())
        ],
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        for path, content in sorted(files.items()):
            if path == "domains/link.json":
                member = _canonical_member(path)
                member.external_attr = 0o120777 << 16
                archive.writestr(member, content)
            else:
                archive.writestr(_canonical_member(path), content)
        archive.writestr(_canonical_member("manifest.json"), json.dumps(manifest))

    with pytest.raises(ReleaseArchiveError, match="symbolic link"):
        _validate(output.getvalue())


@pytest.mark.parametrize("alias", ["domains//widgets.json", "domains/./widgets.json"])
def test_normalized_path_aliases_fail_closed(alias: str) -> None:
    content = _archive(
        mutate_files=lambda files: files.update({alias: files["domains/widgets.json"]})
    )

    with pytest.raises(ReleaseArchiveError, match="canonical path"):
        _validate(content)


def test_duplicate_manifest_json_keys_fail_closed() -> None:
    raw = (
        '{"schema_version":1,"schema_version":1,"version":"'
        + VERSION
        + '","generated_at":"'
        + GENERATED_AT
        + '","git_sha":"'
        + COMMIT
        + '","files":[]}'
    ).encode()

    with pytest.raises(ReleaseArchiveError, match="duplicate JSON key"):
        _validate(_archive(raw_manifest=raw))


@pytest.mark.parametrize(
    "malformed",
    [
        b'{"value": NaN}',
        b'{"value": 1, "value": 2}',
    ],
)
def test_release_json_rejects_noncanonical_values_and_duplicate_keys(malformed: bytes) -> None:
    content = _archive(mutate_files=lambda files: files.update({"domains/widgets.json": malformed}))

    with pytest.raises(ReleaseArchiveError, match="JSON"):
        _validate(content)
