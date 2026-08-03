"""Strict release ZIP manifest and provenance contracts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from collections.abc import Callable

import pytest

from scripts import install_release
from scripts.install_release import (
    ReleaseInstallError,
    install_release_archive,
    write_new_file_atomically,
)
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
    assert (target.stat().st_mode & 0o777) == 0o700
    assert all(
        (path.stat().st_mode & 0o777) == 0o700 for path in target.rglob("*") if path.is_dir()
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
    assert not list(tmp_path.iterdir())


def test_release_install_commit_cannot_replace_a_concurrent_empty_target(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "installed"
    original_check = vars(install_release)["_require_new_target"]
    checks = 0
    concurrent_inode = 0

    def create_target_after_final_check(path) -> None:
        nonlocal checks, concurrent_inode
        original_check(path)
        checks += 1
        if checks == 3:
            target.mkdir()
            concurrent_inode = target.stat().st_ino

    monkeypatch.setattr(install_release, "_require_new_target", create_target_after_final_check)

    with pytest.raises(ReleaseInstallError, match="already exists"):
        install_release_archive(
            _archive(),
            target,
            expected_version=VERSION,
            expected_commit=COMMIT,
        )

    assert target.stat().st_ino == concurrent_inode
    assert not list(target.iterdir())


def test_release_install_rejects_a_symlink_anywhere_in_target_ancestry(tmp_path) -> None:
    real_parent = tmp_path / "real"
    nested = real_parent / "nested"
    nested.mkdir(parents=True)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ReleaseInstallError, match="symlink"):
        install_release_archive(
            _archive(),
            linked_parent / "nested" / "installed",
            expected_version=VERSION,
            expected_commit=COMMIT,
        )

    assert not (nested / "installed").exists()


def test_release_install_normalizes_private_directory_creation_failure(
    tmp_path, monkeypatch
) -> None:
    def deny_creation(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(install_release, "_create_private_install_directory", deny_creation)

    with pytest.raises(ReleaseInstallError, match="temporary directory"):
        install_release_archive(
            _archive(),
            tmp_path / "installed",
            expected_version=VERSION,
            expected_commit=COMMIT,
        )


def test_release_install_cleans_up_when_reserved_directory_cannot_be_opened(
    tmp_path, monkeypatch
) -> None:
    original_open = install_release.os.open
    denied = False

    def deny_reserved_directory(path, flags, *args, dir_fd=None, **kwargs):
        nonlocal denied
        if not denied and isinstance(path, str) and path.startswith(".installed.install-"):
            denied = True
            raise PermissionError("denied")
        return original_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(install_release.os, "open", deny_reserved_directory)

    with pytest.raises(ReleaseInstallError, match="transaction failed"):
        install_release_archive(
            _archive(),
            tmp_path / "installed",
            expected_version=VERSION,
            expected_commit=COMMIT,
        )

    assert not list(tmp_path.glob(".installed.install-*"))


def test_release_install_opens_parent_ancestry_one_component_at_a_time(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "nested" / "installed"
    target.parent.mkdir()
    original_open = install_release.os.open
    opens: list[tuple[str, int | None]] = []

    def record_open(path, flags, *args, dir_fd=None, **kwargs):
        opens.append((os.fspath(path), dir_fd))
        return original_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(install_release.os, "open", record_open)

    install_release_archive(
        _archive(),
        target,
        expected_version=VERSION,
        expected_commit=COMMIT,
    )

    assert (os.fspath(target.parent), None) not in opens
    assert any(path == target.parent.name and dir_fd is not None for path, dir_fd in opens)


def test_release_install_fails_if_opened_parent_is_detached_before_commit(
    tmp_path, monkeypatch
) -> None:
    requested_root = tmp_path / "safe"
    target = requested_root / "nested" / "installed"
    target.parent.mkdir(parents=True)
    parked_root = tmp_path / "safe-parked"
    original_open_parent = vars(install_release)["_open_safe_parent"]

    def detach_after_open(path):
        absolute, parent_fd = original_open_parent(path)
        requested_root.rename(parked_root)
        (requested_root / "nested").mkdir(parents=True)
        return absolute, parent_fd

    monkeypatch.setattr(install_release, "_open_safe_parent", detach_after_open)

    with pytest.raises(ReleaseInstallError, match="parent changed"):
        install_release_archive(
            _archive(),
            target,
            expected_version=VERSION,
            expected_commit=COMMIT,
        )

    assert not target.exists()
    assert not (parked_root / "nested" / "installed").exists()


def test_release_install_rolls_back_if_parent_is_detached_during_commit(
    tmp_path, monkeypatch
) -> None:
    requested_root = tmp_path / "safe"
    target = requested_root / "nested" / "installed"
    target.parent.mkdir(parents=True)
    parked_root = tmp_path / "safe-parked"
    original_commit = vars(install_release)["_atomic_commit_no_replace"]

    def detach_after_commit(parent_fd, temporary_name, target_name) -> None:
        original_commit(parent_fd, temporary_name, target_name)
        requested_root.rename(parked_root)
        (requested_root / "nested").mkdir(parents=True)

    monkeypatch.setattr(install_release, "_atomic_commit_no_replace", detach_after_commit)

    with pytest.raises(ReleaseInstallError, match="parent changed"):
        install_release_archive(
            _archive(),
            target,
            expected_version=VERSION,
            expected_commit=COMMIT,
        )

    assert not target.exists()
    assert not (parked_root / "nested" / "installed").exists()


def test_release_install_cleanup_failure_preserves_the_root_cause(tmp_path, monkeypatch) -> None:
    def fail_write(*_args, **_kwargs):
        raise ReleaseInstallError("measured write failure")

    def fail_cleanup(*_args, **_kwargs):
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(install_release, "_write_entries", fail_write)
    monkeypatch.setattr(install_release.shutil, "rmtree", fail_cleanup)

    with pytest.raises(
        ReleaseInstallError,
        match="measured write failure.*cleanup failed",
    ):
        install_release_archive(
            _archive(),
            tmp_path / "installed",
            expected_version=VERSION,
            expected_commit=COMMIT,
        )


def test_release_install_rejects_unexpected_empty_directories(tmp_path, monkeypatch) -> None:
    original_write = vars(install_release)["_write_entries"]

    def add_unexpected_directory(temporary_fd, entries) -> None:
        original_write(temporary_fd, entries)
        os.mkdir("unexpected", dir_fd=temporary_fd)

    monkeypatch.setattr(install_release, "_write_entries", add_unexpected_directory)

    with pytest.raises(ReleaseInstallError, match="unexpected directories"):
        install_release_archive(
            _archive(),
            tmp_path / "installed",
            expected_version=VERSION,
            expected_commit=COMMIT,
        )


def test_atomic_file_commit_cannot_replace_a_concurrent_target(tmp_path, monkeypatch) -> None:
    target = tmp_path / "receipt.json"
    original_check = vars(install_release)["_require_new_target"]
    checks = 0
    concurrent_inode = 0

    def create_target_after_final_check(path) -> None:
        nonlocal checks, concurrent_inode
        original_check(path)
        checks += 1
        if checks == 3:
            target.write_bytes(b"owner-data")
            concurrent_inode = target.stat().st_ino

    monkeypatch.setattr(install_release, "_require_new_target", create_target_after_final_check)

    with pytest.raises(ReleaseInstallError, match="already exists"):
        write_new_file_atomically(b"receipt", target)

    assert target.stat().st_ino == concurrent_inode
    assert target.read_bytes() == b"owner-data"


def test_atomic_file_write_rolls_back_if_parent_is_detached_during_commit(
    tmp_path, monkeypatch
) -> None:
    requested_root = tmp_path / "safe"
    target = requested_root / "nested" / "receipt.json"
    target.parent.mkdir(parents=True)
    parked_root = tmp_path / "safe-parked"
    original_commit = vars(install_release)["_atomic_commit_no_replace"]

    def detach_after_commit(parent_fd, temporary_name, target_name) -> None:
        original_commit(parent_fd, temporary_name, target_name)
        requested_root.rename(parked_root)
        (requested_root / "nested").mkdir(parents=True)

    monkeypatch.setattr(install_release, "_atomic_commit_no_replace", detach_after_commit)

    with pytest.raises(ReleaseInstallError, match="parent changed"):
        write_new_file_atomically(b"receipt", target)

    assert not target.exists()
    assert not (parked_root / "nested" / "receipt.json").exists()


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
