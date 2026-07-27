"""Generated files must end with a trailing newline.

``release/specs/CHANGELOG.md`` and ``release/specs/.spec_metadata.json`` are build products that
are also committed, and both were written without a trailing newline -- so markdownlint (MD047) and
editorconfig-checker failed on an untouched ``main`` (#716).

Editing the committed files by hand would not have fixed anything: the next pipeline run rewrites
both. These tests pin the behaviour at the two writers instead, so a future refactor cannot
silently drop the newline again and quietly turn the local gate red for everyone.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.download import save_metadata
from scripts.reconcile import SpecReconciler

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestChangelogEndsWithNewline:
    """``reconcile`` writes ``release/specs/CHANGELOG.md``."""

    def test_generate_changelog_ends_with_exactly_one_newline(self, tmp_path):
        # output_dir is created by the constructor, so keep it out of the repo.
        reconciler = SpecReconciler(
            original_dir=REPO_ROOT / "specs" / "transformed",
            output_dir=tmp_path / "out",
        )
        changelog = reconciler.generate_changelog()

        assert changelog.endswith("\n"), (
            "CHANGELOG.md is checked by markdownlint MD047, which requires a single trailing "
            "newline. The generated text ends without one."
        )
        assert not changelog.endswith("\n\n"), (
            "MD047 wants exactly one trailing newline, not a blank line before EOF"
        )

    def test_the_committed_changelog_conforms(self):
        """The artifact in the tree must already satisfy the rule the writer now enforces."""
        committed = (REPO_ROOT / "release" / "specs" / "CHANGELOG.md").read_text()
        assert committed.endswith("\n") and not committed.endswith("\n\n")


class TestSpecMetadataEndsWithNewline:
    """``download`` writes ``.spec_metadata.json``."""

    def test_save_metadata_writes_a_trailing_newline(self, tmp_path):
        save_metadata(
            output_dir=tmp_path,
            etag='W/"test"',
            last_modified="Fri, 24 Jul 2026 17:30:26 GMT",
            file_count=3,
        )
        written = (tmp_path / ".spec_metadata.json").read_text()

        assert written.endswith("\n"), (
            ".spec_metadata.json is checked by editorconfig-checker, which requires a final "
            "newline. json.dump does not emit one."
        )
        assert not written.endswith("\n\n")
        # Still valid JSON, and the newline is the only change.
        assert json.loads(written)["file_count"] == 3

    def test_the_committed_metadata_conforms(self):
        committed = (REPO_ROOT / "release" / "specs" / ".spec_metadata.json").read_text()
        assert committed.endswith("\n") and not committed.endswith("\n\n")
