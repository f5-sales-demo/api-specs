"""Tests that the reconciled output directory mirrors the current input set.

``release/specs`` is a build product that is also committed, so each run wrote
on top of the previous one. Upstream filenames carry a sequence index that
shifts whenever F5 adds or removes a domain, so specs from an earlier run
survived under names the current run never writes and were then packaged into
the published release.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.reconcile import SpecReconciler


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    original = tmp_path / "transformed"
    output = tmp_path / "release"
    original.mkdir()
    output.mkdir()
    return original, output


def _write_spec(directory: Path, name: str, title: str) -> None:
    (directory / name).write_text(
        json.dumps({"openapi": "3.0.0", "info": {"title": title}, "paths": {}})
    )


def test_stale_specs_are_pruned(dirs: tuple[Path, Path]) -> None:
    """A spec left over from an earlier run is removed, not republished."""
    original, output = dirs
    _write_spec(original, "docs.0001.current.json", "current")
    _write_spec(output, "docs.0001.current.json", "previous")
    _write_spec(output, "docs.0198.renumbered.json", "stale")

    reconciler = SpecReconciler(original_dir=original, output_dir=output)
    reconciler.reconcile_all([])
    saved = reconciler.save_results()

    assert set(saved) == {"docs.0001.current.json"}
    assert sorted(p.name for p in output.glob("*.json")) == ["docs.0001.current.json"]


def test_non_spec_files_are_kept(dirs: tuple[Path, Path]) -> None:
    """CHANGELOG.md and the dot-file metadata survive the prune."""
    original, output = dirs
    _write_spec(original, "docs.0001.current.json", "current")
    (output / "CHANGELOG.md").write_text("# Changelog\n")
    (output / ".spec_metadata.json").write_text('{"spec_date": "2026.07.24"}')

    reconciler = SpecReconciler(original_dir=original, output_dir=output)
    reconciler.reconcile_all([])
    reconciler.save_results()

    assert (output / "CHANGELOG.md").exists()
    assert (output / ".spec_metadata.json").exists()


def test_prune_is_idempotent(dirs: tuple[Path, Path]) -> None:
    """Running twice over the same input leaves the same file set."""
    original, output = dirs
    _write_spec(original, "docs.0001.current.json", "current")

    for _ in range(2):
        reconciler = SpecReconciler(original_dir=original, output_dir=output)
        reconciler.reconcile_all([])
        reconciler.save_results()

    assert sorted(p.name for p in output.glob("*.json")) == ["docs.0001.current.json"]
