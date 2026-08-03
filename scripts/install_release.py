"""Install a validated api-specs release archive without overwriting local state."""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import shutil
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
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


DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
FILE_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
TEMPORARY_ATTEMPTS = 16
INSTALL_DIRECTORY_MODE = 0o700


@contextmanager
def _install_validation_boundary() -> Iterator[None]:
    try:
        yield
    except ReleaseArchiveError as error:
        raise ReleaseInstallError(str(error)) from error


def _require_new_target(target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise ReleaseInstallError("release install target already exists")
    absolute = Path(os.path.abspath(target))
    if not absolute.name:
        raise ReleaseInstallError("release install target requires a safe existing parent")
    for ancestor in (absolute.parent, *absolute.parent.parents):
        try:
            metadata = ancestor.lstat()
        except OSError as error:
            raise ReleaseInstallError(
                "release install target requires a safe existing parent"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ReleaseInstallError("release install target ancestry contains a symlink")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ReleaseInstallError("release install target requires a safe existing parent")


def _open_anchored_directory(directory: Path) -> int:
    components = directory.parts
    if not components or components[0] != directory.anchor:
        raise ReleaseInstallError("release install target parent is unsafe")
    current_fd = os.open(directory.anchor, DIRECTORY_FLAGS)
    try:
        for component in components[1:]:
            next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError:
        os.close(current_fd)
        raise


def _require_parent_identity(absolute_target: Path, parent_fd: int) -> None:
    current_fd: int | None = None
    try:
        current_fd = _open_anchored_directory(absolute_target.parent)
        if not os.path.samestat(os.fstat(parent_fd), os.fstat(current_fd)):
            raise ReleaseInstallError("release install target parent changed during transaction")
    except OSError as error:
        raise ReleaseInstallError(
            "release install target parent changed during transaction"
        ) from error
    finally:
        if current_fd is not None:
            os.close(current_fd)


def _open_safe_parent(target: Path) -> tuple[Path, int]:
    absolute = Path(os.path.abspath(target))
    _require_new_target(absolute)
    parent_fd: int | None = None
    try:
        parent_fd = _open_anchored_directory(absolute.parent)
        try:
            os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ReleaseInstallError("release install target already exists")
        _require_new_target(absolute)
    except OSError as error:
        if parent_fd is not None:
            os.close(parent_fd)
        raise ReleaseInstallError("release install target parent is unsafe") from error
    except ReleaseInstallError:
        if parent_fd is not None:
            os.close(parent_fd)
        raise
    if parent_fd is None:
        raise ReleaseInstallError("release install target parent could not be opened")
    return absolute, parent_fd


def _create_private_install_directory(parent_fd: int, target_name: str) -> str:
    for _attempt in range(TEMPORARY_ATTEMPTS):
        temporary_name = f".{target_name}.install-{secrets.token_hex(8)}"
        try:
            os.mkdir(temporary_name, mode=INSTALL_DIRECTORY_MODE, dir_fd=parent_fd)
            return temporary_name
        except FileExistsError:
            continue
    raise ReleaseInstallError("release install could not reserve a private temporary directory")


def _open_member_directory(root_fd: int, parts: tuple[str, ...], *, create: bool) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=INSTALL_DIRECTORY_MODE, dir_fd=current_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError:
        os.close(current_fd)
        raise


def _write_entries(temporary_fd: int, entries: dict[str, bytes]) -> None:
    for archive_path, content in entries.items():
        parts = PurePosixPath(archive_path).parts
        directory_fd = _open_member_directory(temporary_fd, parts[:-1], create=True)
        try:
            file_fd = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | FILE_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                installed = os.fdopen(file_fd, "wb")
            except BaseException:
                os.close(file_fd)
                raise
            with installed:
                installed.write(content)
                installed.flush()
                os.fchmod(installed.fileno(), 0o644)
        except OSError as error:
            raise ReleaseInstallError(
                f"release member could not be installed: {archive_path}"
            ) from error
        finally:
            os.close(directory_fd)


def _installed_tree(temporary_fd: int) -> tuple[set[str], set[str]]:
    installed_paths: set[str] = set()
    installed_directories: set[str] = set()
    for root, directories, filenames, root_fd in os.fwalk(
        ".", follow_symlinks=False, dir_fd=temporary_fd
    ):
        prefix = "" if root == "." else f"{root.removeprefix('./')}/"
        for directory in directories:
            metadata = os.stat(directory, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ReleaseInstallError("installed release contains a non-directory path")
            installed_directories.add(f"{prefix}{directory}")
        for filename in filenames:
            metadata = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise ReleaseInstallError("installed release contains a non-regular member")
            installed_paths.add(f"{prefix}{filename}")
    return installed_paths, installed_directories


def _read_installed_entry(temporary_fd: int, archive_path: str) -> bytes:
    parts = PurePosixPath(archive_path).parts
    directory_fd = _open_member_directory(temporary_fd, parts[:-1], create=False)
    try:
        file_fd = os.open(parts[-1], os.O_RDONLY | FILE_NOFOLLOW, dir_fd=directory_fd)
        with os.fdopen(file_fd, "rb") as installed:
            return installed.read()
    finally:
        os.close(directory_fd)


def _verify_installed_entries(temporary_fd: int, entries: dict[str, bytes]) -> None:
    installed_paths, installed_directories = _installed_tree(temporary_fd)
    if installed_paths != set(entries):
        raise ReleaseInstallError("installed release does not contain the exact archive members")
    expected_directories = {
        parent.as_posix()
        for archive_path in entries
        for parent in PurePosixPath(archive_path).parents
        if parent != PurePosixPath(".")
    }
    if installed_directories != expected_directories:
        raise ReleaseInstallError("installed release contains unexpected directories")
    for archive_path, expected in entries.items():
        try:
            actual = _read_installed_entry(temporary_fd, archive_path)
        except OSError as error:
            raise ReleaseInstallError(
                f"installed release member is unreadable: {archive_path}"
            ) from error
        if actual != expected:
            raise ReleaseInstallError(
                f"installed release member does not match the archive: {archive_path}"
            )


def _normalize_directory_modes(temporary_fd: int) -> None:
    for _root, _directories, _filenames, root_fd in os.fwalk(
        ".", follow_symlinks=False, dir_fd=temporary_fd
    ):
        os.fchmod(root_fd, INSTALL_DIRECTORY_MODE)


def _atomic_commit_no_replace(
    parent_fd: int,
    temporary_name: str,
    target_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(temporary_name)
    target = os.fsencode(target_name)
    if sys.platform == "darwin":
        rename = libc.renameatx_np
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(parent_fd, source, parent_fd, target, 0x00000004)
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as error:
            raise ReleaseInstallError(
                "atomic no-replace installation is unsupported on this platform"
            ) from error
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(parent_fd, source, parent_fd, target, 1)
    else:
        raise ReleaseInstallError("atomic no-replace installation is unsupported on this platform")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise ReleaseInstallError("release install target already exists")
    raise OSError(error_number, os.strerror(error_number), target_name)


def _cleanup_failed_install(
    parent_fd: int,
    temporary_name: str,
    root_error: BaseException,
) -> None:
    try:
        shutil.rmtree(temporary_name, dir_fd=parent_fd)
    except OSError:
        raise ReleaseInstallError(f"{root_error}; cleanup failed") from root_error


def write_new_file_atomically(content: bytes, target: Path) -> None:
    """Write bytes through a private file and atomically commit without replacement."""
    absolute_target, parent_fd = _open_safe_parent(target)
    temporary_name = f".{absolute_target.name}.write-{secrets.token_hex(8)}"
    temporary_exists = False
    try:
        try:
            file_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | FILE_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            temporary_exists = True
            try:
                output = os.fdopen(file_fd, "wb")
            except BaseException:
                os.close(file_fd)
                raise
            with output:
                output.write(content)
                output.flush()
                os.fchmod(output.fileno(), 0o644)
            _require_new_target(absolute_target)
            _require_parent_identity(absolute_target, parent_fd)
            _atomic_commit_no_replace(parent_fd, temporary_name, absolute_target.name)
            temporary_exists = False
            try:
                _require_parent_identity(absolute_target, parent_fd)
            except ReleaseInstallError as error:
                try:
                    os.unlink(absolute_target.name, dir_fd=parent_fd)
                except OSError:
                    raise ReleaseInstallError(f"{error}; rollback failed") from error
                raise
        except (OSError, ReleaseInstallError) as error:
            if temporary_exists:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except OSError:
                    raise ReleaseInstallError(f"{error}; cleanup failed") from error
            if isinstance(error, ReleaseInstallError):
                raise
            raise ReleaseInstallError("atomic output write failed") from error
    finally:
        os.close(parent_fd)


def install_release_archive(
    content: bytes,
    target: Path,
    *,
    expected_version: str,
    expected_commit: str,
) -> ReleaseInstallation:
    """Validate and atomically install the exact release bytes into a new directory."""
    with _install_validation_boundary():
        validated = validate_release_archive_bytes(
            content,
            expected_version=expected_version,
            expected_commit=expected_commit,
        )

    absolute_target, parent_fd = _open_safe_parent(target)
    temporary_name: str | None = None
    try:
        try:
            temporary_name = _create_private_install_directory(parent_fd, absolute_target.name)
            temporary_fd = os.open(temporary_name, DIRECTORY_FLAGS, dir_fd=parent_fd)
            try:
                _write_entries(temporary_fd, validated.entries)
                _verify_installed_entries(temporary_fd, validated.entries)
                _normalize_directory_modes(temporary_fd)
            finally:
                os.close(temporary_fd)
            _require_new_target(absolute_target)
            _require_parent_identity(absolute_target, parent_fd)
            _atomic_commit_no_replace(parent_fd, temporary_name, absolute_target.name)
            temporary_name = None
            try:
                _require_parent_identity(absolute_target, parent_fd)
            except ReleaseInstallError as error:
                try:
                    shutil.rmtree(absolute_target.name, dir_fd=parent_fd)
                except OSError:
                    raise ReleaseInstallError(f"{error}; rollback failed") from error
                raise
        except (OSError, ReleaseInstallError) as error:
            if temporary_name is not None:
                _cleanup_failed_install(parent_fd, temporary_name, error)
            if isinstance(error, ReleaseInstallError):
                raise
            if temporary_name is None:
                raise ReleaseInstallError(
                    "release install could not create a private temporary directory"
                ) from error
            raise ReleaseInstallError("release install transaction failed") from error
    finally:
        os.close(parent_fd)

    return ReleaseInstallation(
        target=target,
        file_count=len(validated.entries),
        installed_bytes=sum(map(len, validated.entries.values())),
    )
