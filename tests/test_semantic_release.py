"""Semantic release identity and measured release-note contracts."""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import UTC, datetime
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


def _receipt(version: str) -> dict:
    return {
        "version": version,
        "tag_name": f"v{version}",
        "published_at": "2026-08-02T08:25:00Z",
        "asset_name": f"api-specs-v{version}.zip",
        "asset_size": 123,
        "asset_digest": "sha256:" + "1" * 64,
    }


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
    files = {
        f"domains/{domain_name}": json.dumps(domain, indent=2).encode(),
        "openapi.json": json.dumps(aggregate, indent=2).encode(),
        "openapi.yaml": yaml.safe_dump(aggregate, sort_keys=False).encode(),
        "VALIDATION_REPORT.md": report.encode(),
        "CHANGELOG.md": b"# Changelog\n\n- Stable correction.\n",
    }
    manifest = {
        "schema_version": 1,
        "version": version,
        "generated_at": generated_at,
        "git_sha": git_sha,
        "files": [
            {
                "path": member,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for member, content in sorted(files.items())
        ],
    }

    payloads = {**files, "manifest.json": json.dumps(manifest, indent=2).encode()}
    timestamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00")).astimezone(UTC)
    zip_timestamp = (
        timestamp.year,
        timestamp.month,
        timestamp.day,
        timestamp.hour,
        timestamp.minute,
        timestamp.second - timestamp.second % 2,
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        for member, content in sorted(payloads.items()):
            info = zipfile.ZipInfo(member, date_time=zip_timestamp)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.internal_attr = 0
            info.extra = b""
            info.comment = b""
            archive.writestr(info, content, compress_type=zipfile.ZIP_STORED)
    return path


def _snapshot(path: Path, version: str, commit: str) -> dict:
    return semantic_snapshot_from_archive(
        path,
        expected_version=version,
        expected_commit=commit,
    )


def test_release_only_version_commit_timestamp_and_derived_sizes_are_not_semantic(
    tmp_path: Path,
) -> None:
    first = _archive(
        tmp_path / "first.zip",
        version="2026.07.30-18",
        generated_at="2026-08-02T05:30:00+00:00",
        git_sha="a" * 40,
    )
    second = _archive(
        tmp_path / "second.zip",
        version="2026.07.31-1",
        generated_at="2026-08-02T08:25:00+00:00",
        git_sha="b" * 40,
    )

    first_snapshot = _snapshot(first, "2026.07.30-18", "a" * 40)
    second_snapshot = _snapshot(second, "2026.07.31-1", "b" * 40)

    assert first_snapshot == second_snapshot
    assert compare_snapshots(second_snapshot, first_snapshot)["changed"] is False


def test_semantic_snapshot_requires_external_version_and_commit_identity(
    tmp_path: Path,
) -> None:
    archive = _archive(
        tmp_path / "candidate.zip",
        version="2026.07.30-18",
        generated_at="2026-08-02T05:30:00+00:00",
        git_sha="a" * 40,
    )

    with pytest.raises(SemanticReleaseError, match="version"):
        _snapshot(archive, "2026.07.30-19", "a" * 40)
    with pytest.raises(SemanticReleaseError, match="commit"):
        _snapshot(archive, "2026.07.30-18", "b" * 40)


def test_domain_and_nonmetadata_generated_changes_are_measured(tmp_path: Path) -> None:
    previous = _snapshot(
        _archive(
            tmp_path / "previous.zip",
            version="2026.07.30-18",
            generated_at="2026-08-02T05:30:00+00:00",
            git_sha="a" * 40,
        ),
        "2026.07.30-18",
        "a" * 40,
    )
    current = _snapshot(
        _archive(
            tmp_path / "current.zip",
            version="2026.07.30-19",
            generated_at="2026-08-02T08:25:00+00:00",
            git_sha="b" * 40,
            domain_path="/widgets-v2",
            report_passed=2,
        ),
        "2026.07.30-19",
        "b" * 40,
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
    previous = _snapshot(
        _archive(
            tmp_path / "previous.zip",
            version="2026.07.30-18",
            generated_at="2026-08-02T05:30:00+00:00",
            git_sha="a" * 40,
        ),
        "2026.07.30-18",
        "a" * 40,
    )
    current = _snapshot(
        _archive(
            tmp_path / "current.zip",
            version="2026.07.30-19",
            generated_at="2026-08-02T08:25:00+00:00",
            git_sha="b" * 40,
            domain_path="/widgets-v2",
        ),
        "2026.07.30-19",
        "b" * 40,
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
        git_sha="b" * 40,
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
        expected_commit="b" * 40,
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
                expected_commit="b" * 40,
            )


class _Response:
    def __init__(self, status: int, payload: object) -> None:
        self.status_code = status
        self.payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self.payload


def _response(status: int, payload: object) -> _Response:
    return _Response(status, payload)


def test_missing_latest_release_bootstraps_only_from_proven_empty_state(monkeypatch) -> None:
    responses = iter(
        [
            _response(404, {}),
            _response(200, []),
            _response(200, []),
        ]
    )
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: next(responses))

    assert _latest_verified_snapshot("f5-sales-demo/api-specs", "token") is None


def test_missing_latest_release_rejects_legacy_release_or_tag_state(monkeypatch) -> None:
    for releases, tags in (([{"id": 1}], []), ([], [{"ref": "refs/tags/vlegacy"}])):
        responses = iter(
            [
                _response(404, {}),
                _response(200, releases),
                _response(200, tags),
            ]
        )
        monkeypatch.setattr(
            requests,
            "get",
            lambda *_args, responses=responses, **_kwargs: next(responses),
        )
        with pytest.raises(SemanticReleaseError, match="clean bootstrap"):
            _latest_verified_snapshot("f5-sales-demo/api-specs", "token")


def test_empty_repository_creates_a_measured_baseline_release(tmp_path: Path) -> None:
    commit = "a" * 40
    snapshot = _snapshot(
        _archive(
            tmp_path / "baseline.zip",
            version="2026.07.30-1",
            generated_at="2026-07-30T15:32:52+00:00",
            git_sha=commit,
        ),
        "2026.07.30-1",
        commit,
    )

    decision = decide_publication(
        snapshot,
        None,
        candidate_version="2026.07.30-1",
        source_commit=commit,
    )

    assert decision["publication_mode"] == "create"
    assert decision["added_domains"] == ["widgets"]
    assert decision["previous_semantic_digest"] == "none"
    assert decision["previous_release_tag"] == ""


def test_published_release_with_failed_dispatch_recovers_on_later_run(tmp_path: Path) -> None:
    source_commit = "a" * 40
    previous_snapshot = _snapshot(
        _archive(
            tmp_path / "previous.zip",
            version="2026.07.30-18",
            generated_at="2026-07-30T15:32:52+00:00",
            git_sha="b" * 40,
        ),
        "2026.07.30-18",
        "b" * 40,
    )
    current_snapshot = _snapshot(
        _archive(
            tmp_path / "current.zip",
            version="2026.07.30-19",
            generated_at="2026-07-30T15:32:52+00:00",
            git_sha=source_commit,
            domain_path="/widgets-v2",
        ),
        "2026.07.30-19",
        source_commit,
    )
    prior_release = VerifiedLatestRelease(
        tag="v2026.07.30-18",
        commit="b" * 40,
        asset_name="api-specs-v2026.07.30-18.zip",
        content=b"previous release",
        snapshot=previous_snapshot,
        receipt=_receipt("2026.07.30-18"),
        delivery_acknowledged=True,
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
        receipt=_receipt("2026.07.30-19"),
        delivery_acknowledged=False,
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


def test_acknowledged_release_is_not_redispatched_from_a_later_commit(tmp_path: Path) -> None:
    snapshot = _snapshot(
        _archive(
            tmp_path / "latest.zip",
            version="2026.07.30-19",
            generated_at="2026-07-30T15:32:52+00:00",
            git_sha="a" * 40,
        ),
        "2026.07.30-19",
        "a" * 40,
    )
    latest = VerifiedLatestRelease(
        tag="v2026.07.30-19",
        commit="a" * 40,
        asset_name="api-specs-v2026.07.30-19.zip",
        content=b"release",
        snapshot=snapshot,
        receipt=_receipt("2026.07.30-19"),
        delivery_acknowledged=True,
    )

    decision = decide_publication(
        snapshot,
        latest,
        candidate_version="2026.07.30-20",
        source_commit="b" * 40,
    )

    assert decision["publication_mode"] == "none"
    assert decision["release_version"] == ""


def test_unacknowledged_release_recovers_across_later_nonsemantic_commit(
    tmp_path: Path,
) -> None:
    release_commit = "a" * 40
    later_commit = "b" * 40
    snapshot = _snapshot(
        _archive(
            tmp_path / "latest.zip",
            version="2026.07.30-19",
            generated_at="2026-07-30T15:32:52+00:00",
            git_sha=release_commit,
        ),
        "2026.07.30-19",
        release_commit,
    )
    latest = VerifiedLatestRelease(
        tag="v2026.07.30-19",
        commit=release_commit,
        asset_name="api-specs-v2026.07.30-19.zip",
        content=b"release",
        snapshot=snapshot,
        receipt=_receipt("2026.07.30-19"),
        delivery_acknowledged=False,
    )

    decision = decide_publication(
        snapshot,
        latest,
        candidate_version="2026.07.30-20",
        source_commit=later_commit,
    )

    assert decision["publication_mode"] == "recover"
    assert decision["release_version"] == "2026.07.30-19"
    assert decision["release_commit"] == release_commit
