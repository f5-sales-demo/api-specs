"""Semantic release identity and measured release-note contracts."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
import requests
import yaml

from scripts.semantic_release import (
    SemanticReleaseError,
    VerifiedLatestRelease,
    _latest_verified_snapshot,
    compare_snapshots,
    decide_publication,
    render_release_notes,
    semantic_snapshot_from_archive,
    validate_previous_release_asset,
)


def _openapi(version: str, *, path: str = "/widgets") -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "Widgets", "version": version},
        "paths": {path: {"get": {"operationId": "listWidgets"}}},
        "components": {"schemas": {}},
    }


def _archive(
    path: Path,
    *,
    version: str,
    generated_at: str,
    git_sha: str,
    domain_path: str = "/widgets",
    report_passed: int = 1,
) -> Path:
    domain_name = "docs-cloud-f5-com.0001.public.ves.io.schema.widgets.ves-swagger.json"
    domain = _openapi(version, path=domain_path)
    aggregate = _openapi(version, path=domain_path)
    report = (
        "# F5 XC API Validation Report\n\n"
        f"**Generated:** {generated_at}\n\n"
        "## Summary\n\n"
        f"- **Passed:** {report_passed}\n"
    )
    manifest = {
        "version": version,
        "generated_at": generated_at,
        "git_sha": git_sha,
        "files": [
            {"path": f"domains/{domain_name}", "size": len(json.dumps(domain))},
            {"path": "openapi.json", "size": len(json.dumps(aggregate))},
            {"path": "VALIDATION_REPORT.md", "size": len(report)},
        ],
    }

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"domains/{domain_name}", json.dumps(domain, indent=2))
        archive.writestr("openapi.json", json.dumps(aggregate, indent=2))
        archive.writestr("openapi.yaml", yaml.safe_dump(aggregate, sort_keys=False))
        archive.writestr("VALIDATION_REPORT.md", report)
        archive.writestr("CHANGELOG.md", "# Changelog\n\n- Stable correction.\n")
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
    return path


def test_release_only_version_commit_timestamp_and_derived_sizes_are_not_semantic(
    tmp_path: Path,
) -> None:
    first = _archive(
        tmp_path / "first.zip",
        version="2026.07.30-18",
        generated_at="2026-08-02T05:30:00+00:00",
        git_sha="a" * 7,
    )
    second = _archive(
        tmp_path / "second.zip",
        version="2026.07.30-19-longer",
        generated_at="2026-08-02T08:25:00+00:00",
        git_sha="b" * 7,
    )

    first_snapshot = semantic_snapshot_from_archive(first)
    second_snapshot = semantic_snapshot_from_archive(second)

    assert first_snapshot == second_snapshot
    assert compare_snapshots(second_snapshot, first_snapshot)["changed"] is False


def test_domain_and_nonmetadata_generated_changes_are_measured(tmp_path: Path) -> None:
    previous = semantic_snapshot_from_archive(
        _archive(
            tmp_path / "previous.zip",
            version="2026.07.30-18",
            generated_at="2026-08-02T05:30:00+00:00",
            git_sha="a" * 7,
        )
    )
    current = semantic_snapshot_from_archive(
        _archive(
            tmp_path / "current.zip",
            version="2026.07.30-19",
            generated_at="2026-08-02T08:25:00+00:00",
            git_sha="b" * 7,
            domain_path="/widgets-v2",
            report_passed=2,
        )
    )

    decision = compare_snapshots(current, previous)

    assert decision["changed"] is True
    assert decision["modified_domains"] == ["widgets"]
    assert decision["added_domains"] == []
    assert decision["removed_domains"] == []
    assert decision["changed_artifacts"] == [
        "VALIDATION_REPORT.md",
        "openapi.json",
        "openapi.yaml",
    ]


def test_release_notes_state_only_measured_semantic_changes(tmp_path: Path) -> None:
    previous = semantic_snapshot_from_archive(
        _archive(
            tmp_path / "previous.zip",
            version="2026.07.30-18",
            generated_at="2026-08-02T05:30:00+00:00",
            git_sha="a" * 7,
        )
    )
    current = semantic_snapshot_from_archive(
        _archive(
            tmp_path / "current.zip",
            version="2026.07.30-19",
            generated_at="2026-08-02T08:25:00+00:00",
            git_sha="b" * 7,
            domain_path="/widgets-v2",
        )
    )
    decision = compare_snapshots(current, previous)

    notes = render_release_notes(
        decision,
        version="2026.07.30-19",
        specs_etag='W/"example"',
        repository="f5-sales-demo/api-specs",
    )

    assert "Modified domains (1): `widgets`" in notes
    assert "Changed generated artifacts (2): `openapi.json`, `openapi.yaml`" in notes
    assert f"Semantic Digest: {current['semantic_digest']}" in notes
    assert "Code changes resulted in updated output" not in notes
    assert "Upstream F5 XC specs updated" not in notes


def test_previous_release_archive_requires_immutable_exact_size_and_digest(
    tmp_path: Path,
) -> None:
    archive = _archive(
        tmp_path / "api-specs-v2026.07.30-19.zip",
        version="2026.07.30-19",
        generated_at="2026-08-02T08:25:00+00:00",
        git_sha="b" * 7,
    )
    content = archive.read_bytes()
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    release = {
        "tag_name": "v2026.07.30-19",
        "draft": False,
        "prerelease": False,
        "immutable": True,
        "published_at": "2026-08-02T08:25:00Z",
        "assets": [
            {
                "name": archive.name,
                "state": "uploaded",
                "content_type": "application/zip",
                "size": len(content),
                "digest": digest,
                "browser_download_url": (
                    "https://github.com/f5-sales-demo/api-specs/releases/download/"
                    f"v2026.07.30-19/{archive.name}"
                ),
            }
        ],
    }

    validate_previous_release_asset(
        release,
        archive,
        repository="f5-sales-demo/api-specs",
    )

    for update, message in (
        ({"immutable": False}, "immutable"),
        ({"size": len(content) + 1}, "size"),
        ({"digest": "sha256:" + "0" * 64}, "digest"),
    ):
        candidate = json.loads(json.dumps(release))
        if "immutable" in update:
            candidate["immutable"] = update["immutable"]
        else:
            candidate["assets"][0].update(update)
        with pytest.raises(SemanticReleaseError, match=message):
            validate_previous_release_asset(
                candidate,
                archive,
                repository="f5-sales-demo/api-specs",
            )


def test_established_repository_rejects_missing_latest_release(monkeypatch) -> None:
    response = requests.Response()
    response.status_code = 404
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(SemanticReleaseError, match="HTTP 404"):
        _latest_verified_snapshot("f5-sales-demo/api-specs", None)


def test_published_release_with_failed_dispatch_recovers_on_later_run(tmp_path: Path) -> None:
    source_commit = "a" * 40
    previous_snapshot = semantic_snapshot_from_archive(
        _archive(
            tmp_path / "previous.zip",
            version="2026.07.30-18",
            generated_at="2026-07-30T15:32:52+00:00",
            git_sha="b" * 40,
        )
    )
    current_snapshot = semantic_snapshot_from_archive(
        _archive(
            tmp_path / "current.zip",
            version="2026.07.30-19",
            generated_at="2026-07-30T15:32:52+00:00",
            git_sha=source_commit,
            domain_path="/widgets-v2",
        )
    )
    prior_release = VerifiedLatestRelease(
        tag="v2026.07.30-18",
        commit="b" * 40,
        asset_name="api-specs-v2026.07.30-18.zip",
        content=b"previous release",
        snapshot=previous_snapshot,
    )

    initial = decide_publication(
        current_snapshot,
        prior_release,
        candidate_version="2026.07.30-19",
        source_commit=source_commit,
    )
    assert initial["publication_mode"] == "create"
    assert initial["changed"] is True

    # The release was published from this exact commit, but dispatch timed out.
    # A later run sees equal semantics and must verify/dispatch that same release.
    published_release = VerifiedLatestRelease(
        tag="v2026.07.30-19",
        commit=source_commit,
        asset_name="api-specs-v2026.07.30-19.zip",
        content=b"exact immutable published bytes",
        snapshot=current_snapshot,
    )
    retry = decide_publication(
        current_snapshot,
        published_release,
        candidate_version="2026.07.30-20",
        source_commit=source_commit,
    )

    assert retry["changed"] is False
    assert retry["publication_mode"] == "recover"
    assert retry["release_version"] == "2026.07.30-19"
    assert retry["release_asset"] == "api-specs-v2026.07.30-19.zip"


def test_unchanged_semantics_from_a_different_commit_do_not_redispatch(tmp_path: Path) -> None:
    snapshot = semantic_snapshot_from_archive(
        _archive(
            tmp_path / "latest.zip",
            version="2026.07.30-19",
            generated_at="2026-07-30T15:32:52+00:00",
            git_sha="a" * 40,
        )
    )
    latest = VerifiedLatestRelease(
        tag="v2026.07.30-19",
        commit="a" * 40,
        asset_name="api-specs-v2026.07.30-19.zip",
        content=b"release",
        snapshot=snapshot,
    )

    decision = decide_publication(
        snapshot,
        latest,
        candidate_version="2026.07.30-20",
        source_commit="b" * 40,
    )

    assert decision["publication_mode"] == "none"
    assert decision["release_version"] == ""
