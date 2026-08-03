"""Install a validated api-specs release archive without overwriting local state."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from scripts.release_archive import ReleaseArchiveError, validate_release_archive_bytes


class ReleaseInstallError(RuntimeError):
    """Raised when a release cannot be installed exactly and safely."""


@dataclass(frozen=True)
class ReleaseInstallation:
    """Measured result of one exact local artifact installation."""

    target: Path
    file_count: int
    installed_bytes: int


def _require_new_target(target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise ReleaseInstallError("release install target already exists")
    parent = target.parent
    if not target.name or not parent.is_dir() or parent.is_symlink():
        raise ReleaseInstallError("release install target requires a safe existing parent")


def _write_entries(temporary: Path, entries: dict[str, bytes]) -> None:
    for archive_path, content in entries.items():
        destination = temporary.joinpath(*PurePosixPath(archive_path).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open("xb") as installed:
                installed.write(content)
            destination.chmod(0o644)
        except OSError as error:
            raise ReleaseInstallError(
                f"release member could not be installed: {archive_path}"
            ) from error


def _verify_installed_entries(temporary: Path, entries: dict[str, bytes]) -> None:
    installed_paths = {
        path.relative_to(temporary).as_posix()
        for path in temporary.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if installed_paths != set(entries):
        raise ReleaseInstallError("installed release does not contain the exact archive members")
    for archive_path, expected in entries.items():
        installed = temporary.joinpath(*PurePosixPath(archive_path).parts)
        try:
            actual = installed.read_bytes()
        except OSError as error:
            raise ReleaseInstallError(
                f"installed release member is unreadable: {archive_path}"
            ) from error
        if actual != expected:
            raise ReleaseInstallError(
                f"installed release member does not match the archive: {archive_path}"
            )


def _normalize_directory_modes(temporary: Path) -> None:
    for directory in temporary.rglob("*"):
        if directory.is_dir():
            directory.chmod(0o755)
    temporary.chmod(0o755)


def install_release_archive(
    content: bytes,
    target: Path,
    *,
    expected_version: str,
    expected_commit: str,
) -> ReleaseInstallation:
    """Validate and atomically install the exact release bytes into a new directory."""
    _require_new_target(target)
    try:
        validated = validate_release_archive_bytes(
            content,
            expected_version=expected_version,
            expected_commit=expected_commit,
        )
    except ReleaseArchiveError as error:
        raise ReleaseInstallError(str(error)) from error

    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.install-", dir=target.parent))
    committed = False
    try:
        _write_entries(temporary, validated.entries)
        _verify_installed_entries(temporary, validated.entries)
        _normalize_directory_modes(temporary)
        _require_new_target(target)
        temporary.rename(target)
        committed = True
    except OSError as error:
        raise ReleaseInstallError("release install could not be committed atomically") from error
    finally:
        if not committed:
            shutil.rmtree(temporary)

    return ReleaseInstallation(
        target=target,
        file_count=len(validated.entries),
        installed_bytes=sum(map(len, validated.entries.values())),
    )
