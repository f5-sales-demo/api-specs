"""Tests for the VALIDATION_REPORT.md builder in scripts/release.py.

The builder reads a validation_report.json (produced by
ReportGenerator) and, when an issue_mapping.json is supplied, annotates
each discrepancy row with a link to the tracking GitHub issue.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts.release import ReleaseBuilder, build_validation_report_md, get_git_sha, main
from scripts.release_archive import validate_release_archive_bytes
from scripts.utils.constraint_validator import Discrepancy, DiscrepancyType
from scripts.utils.discrepancy_fingerprint import fingerprint


def test_validation_report_contains_tracked_as_issues_column(tmp_path):
    """When a mapping entry exists, the row links to its GitHub issue."""
    validation_report = tmp_path / "validation_report.json"
    validation_report.write_text(
        json.dumps(
            {
                "summary": {
                    "timestamp": "2026-04-21T00:00:00+00:00",
                    "total_endpoints": 1,
                    "total_tests": 1,
                    "passed": 0,
                    "failed": 1,
                    "errors": 0,
                    "total_discrepancies": 1,
                },
                "discrepancies": [
                    {
                        "path": "/public/config/namespaces/system/origin_pools",
                        "property_name": "port",
                        "constraint_type": "minimum",
                        "discrepancy_type": "spec_stricter",
                        "domain": "origin_pool",
                        "method": "POST",
                        "spec_value": 1,
                        "api_behavior": {"accepted": 0},
                        "test_values": [0],
                    }
                ],
            }
        )
    )

    # Compute the fingerprint with the same inputs the builder will use.
    fp = fingerprint(
        Discrepancy(
            path="/public/config/namespaces/system/origin_pools",
            property_name="port",
            constraint_type="minimum",
            discrepancy_type=DiscrepancyType.SPEC_STRICTER,
            spec_value=1,
            api_behavior={"accepted": 0},
            test_values=[0],
        ),
        "origin_pool",
        "POST",
    )
    mapping = {
        fp: {
            "action": "created",
            "issue_number": 42,
            "issue_url": "https://github.com/x/y/issues/42",
        }
    }
    issue_mapping = tmp_path / "issue_mapping.json"
    issue_mapping.write_text(json.dumps(mapping))

    md = build_validation_report_md(
        validation_report,
        "2026-04-20T12:34:56+00:00",
        issue_mapping,
    )

    assert "Tracked as issues" in md
    assert "#42" in md
    assert "https://github.com/x/y/issues/42" in md
    assert "**Generated:** 2026-04-20T12:34:56+00:00" in md
    assert "2026-04-21T00:00:00+00:00" not in md


def test_validation_report_renders_em_dash_when_no_issue_mapped(tmp_path):
    """Rows without a mapping entry show an em-dash in the issue column."""
    validation_report = tmp_path / "validation_report.json"
    validation_report.write_text(
        json.dumps(
            {
                "discrepancies": [
                    {
                        "path": "/x",
                        "property_name": "p",
                        "constraint_type": "minimum",
                        "discrepancy_type": "spec_stricter",
                        "domain": "origin_pool",
                        "method": "POST",
                        "spec_value": 1,
                        "api_behavior": {},
                        "test_values": [0],
                    }
                ]
            }
        )
    )

    md = build_validation_report_md(
        validation_report,
        "2026-04-20T12:34:56+00:00",
        None,
    )

    assert "Tracked as issues" in md
    assert "—" in md  # em-dash for unmapped rows


def test_malformed_validation_entry_fails_closed_and_names_its_index(tmp_path: Path) -> None:
    validation_report = tmp_path / "validation_report.json"
    validation_report.write_text(
        json.dumps(
            {
                "summary": {"total_discrepancies": 2},
                "discrepancies": [
                    {
                        "path": "/valid",
                        "property_name": "port",
                        "constraint_type": "minimum",
                        "discrepancy_type": "spec_stricter",
                        "domain": "origin_pool",
                        "method": "POST",
                        "spec_value": 1,
                        "api_behavior": {},
                        "test_values": [0],
                    },
                    {
                        "path": "/invalid",
                        "constraint_type": "minimum",
                        "discrepancy_type": "spec_stricter",
                        "domain": "origin_pool",
                        "method": "POST",
                        "test_values": [],
                    },
                ],
            }
        )
    )

    with pytest.raises(ValueError, match=r"discrepancies\[1\]\.property_name"):
        build_validation_report_md(
            validation_report,
            "2026-04-20T12:34:56+00:00",
        )


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("path", {"not": "a string"}),
        ("property_name", ["not", "a", "string"]),
        ("constraint_type", {"not": "a string"}),
        ("domain", 7),
        ("method", ["POST"]),
        ("test_values", {"not": "an array"}),
    ],
)
def test_malformed_discrepancy_field_types_fail_closed(
    tmp_path: Path,
    field: str,
    invalid: object,
) -> None:
    discrepancy = {
        "path": "/widgets",
        "property_name": "port",
        "constraint_type": "minimum",
        "discrepancy_type": "spec_stricter",
        "domain": "widgets",
        "method": "POST",
        "spec_value": 1,
        "api_behavior": {"accepted": 0},
        "test_values": [0],
    }
    discrepancy[field] = invalid
    validation_report = tmp_path / "validation_report.json"
    validation_report.write_text(json.dumps({"discrepancies": [discrepancy]}))

    with pytest.raises(
        ValueError,
        match=rf"discrepancies\[0\]\.{field}",
    ):
        build_validation_report_md(
            validation_report,
            "2026-04-20T12:34:56+00:00",
        )


@pytest.mark.parametrize(
    "document, message",
    [
        ({"summary": {}, "discrepancies": {}}, "discrepancies must be an array"),
        (
            {"summary": {"total_discrepancies": 1}, "discrepancies": []},
            "total_discrepancies",
        ),
        ({"summary": [], "discrepancies": []}, "summary must be an object"),
    ],
)
def test_validation_report_schema_is_strict(
    tmp_path: Path,
    document: object,
    message: str,
) -> None:
    validation_report = tmp_path / "validation_report.json"
    validation_report.write_text(json.dumps(document))

    with pytest.raises(ValueError, match=message):
        build_validation_report_md(
            validation_report,
            "2026-04-20T12:34:56+00:00",
        )


@pytest.mark.parametrize(
    "content",
    [
        '{"summary":{},"discrepancies":[],"discrepancies":[]}',
        '{"summary":{"total_discrepancies":NaN},"discrepancies":[]}',
    ],
)
def test_validation_report_rejects_lossy_json(content: str, tmp_path: Path) -> None:
    validation_report = tmp_path / "validation_report.json"
    validation_report.write_text(content)

    with pytest.raises(ValueError, match="duplicate JSON key|finite"):
        build_validation_report_md(
            validation_report,
            "2026-04-20T12:34:56+00:00",
        )


def test_release_builder_requires_explicit_version_and_complete_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signature = inspect.signature(ReleaseBuilder)
    assert signature.parameters["version"].default is inspect.Parameter.empty
    assert signature.parameters["build_timestamp"].default is inspect.Parameter.empty

    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "widgets.json").write_text(
        json.dumps(
            {
                "openapi": "3.0.0",
                "info": {"title": "Widgets", "version": "2026.08.02"},
                "paths": {},
            }
        )
    )
    builder = ReleaseBuilder(
        specs,
        tmp_path / "output",
        version="2026.08.02-1",
        build_timestamp="2026-08-02T07:08:10+00:00",
    )
    builder.artifact_path().write_bytes(b"stale candidate")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(FileNotFoundError, match="CHANGELOG"):
        builder.build()
    assert not builder.artifact_path().exists()
    assert not (tmp_path / "output" / "api-specs-v2026.08.02-1").exists()

    (specs / "CHANGELOG.md").write_text("# Changelog\n")
    with pytest.raises(FileNotFoundError, match="validation report"):
        builder.build()
    assert not (tmp_path / "output" / "api-specs-v2026.08.02-1").exists()


def test_release_cli_has_no_compatibility_fallbacks() -> None:
    source = Path(inspect.getsourcefile(main) or "").read_text()

    for legacy in (
        "--patch",
        "--no-changelog",
        "--no-report",
        "--release-notes",
        "Using original specs",
        "get_version_from_metadata",
        "get_version_from_git",
    ):
        assert legacy not in source


def test_release_build_is_byte_identical_across_source_mtimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.release.get_git_sha", lambda: "a" * 40)
    specs = tmp_path / "specs"
    specs.mkdir()
    widget = specs / "widgets.json"
    widget.write_text(
        json.dumps(
            {
                "openapi": "3.0.0",
                "info": {"title": "Widgets", "version": "2026.08.02"},
                "paths": {"/widgets": {"get": {"operationId": "listWidgets"}}},
            },
            indent=2,
        )
    )
    (specs / "CHANGELOG.md").write_text("# Changelog\n\n- Stable correction.\n")
    # Download provenance is an input to the build clock, not a published domain.
    (specs / ".spec_metadata.json").write_text(
        json.dumps({"spec_timestamp": "2026-08-02T07:08:10+00:00"})
    )
    reports = tmp_path / "reports"
    reports.mkdir()
    validation_report = reports / "validation_report.json"
    validation_report.write_text(
        json.dumps(
            {
                "summary": {
                    "timestamp": "2026-08-02T09:00:00+00:00",
                    "total_endpoints": 1,
                    "total_tests": 1,
                    "passed": 1,
                    "failed": 0,
                    "errors": 0,
                    "total_discrepancies": 0,
                },
                "discrepancies": [],
            }
        )
    )
    monkeypatch.chdir(tmp_path)

    first_builder = ReleaseBuilder(
        specs,
        tmp_path / "first",
        version="2026.08.02-1",
        build_timestamp="2026-08-02T07:08:10+00:00",
    )
    first = first_builder.build().read_bytes()

    os.utime(widget, (1_800_000_000, 1_800_000_000))
    os.utime(specs / "CHANGELOG.md", (1_700_000_000, 1_700_000_000))
    validation_document = json.loads(validation_report.read_text())
    validation_document["summary"]["timestamp"] = "2026-08-02T10:30:00+00:00"
    validation_report.write_text(json.dumps(validation_document))

    second_builder = ReleaseBuilder(
        specs,
        tmp_path / "second",
        version="2026.08.02-1",
        build_timestamp="2026-08-02T07:08:10+00:00",
    )
    second_path = second_builder.build()
    second = second_path.read_bytes()

    assert first == second
    with zipfile.ZipFile(second_path) as archive:
        members = archive.infolist()
        assert [member.filename for member in members] == sorted(
            member.filename for member in members
        )
        assert "domains/.spec_metadata.json" not in archive.namelist()
        assert {member.create_system for member in members} == {3}
        assert {member.compress_type for member in members} == {zipfile.ZIP_STORED}
        assert {(member.external_attr >> 16) & 0o777 for member in members} == {0o644}
        assert {member.date_time for member in members} == {(2026, 8, 2, 7, 8, 10)}
        report = archive.read("VALIDATION_REPORT.md").decode()
        manifest = json.loads(archive.read("manifest.json"))

    assert "**Generated:** 2026-08-02T07:08:10+00:00" in report
    assert "2026-08-02T10:30:00+00:00" not in report
    assert manifest["generated_at"] == "2026-08-02T07:08:10+00:00"
    assert [entry["path"] for entry in manifest["files"]] == sorted(
        entry["path"] for entry in manifest["files"]
    )
    assert len(manifest["git_sha"]) == 40
    validated = validate_release_archive_bytes(
        second,
        expected_version="2026.08.02-1",
        expected_commit="a" * 40,
    )
    assert validated.manifest == manifest


@pytest.mark.parametrize(
    "version",
    ("../escape", "2026.8.02-1", "2026.08.02-0", "2026.02.30-1"),
)
def test_release_version_rejects_noncanonical_and_unsafe_values_before_mutation(
    tmp_path: Path,
    version: str,
) -> None:
    output = tmp_path / "output"

    with pytest.raises(ValueError, match="version"):
        ReleaseBuilder(
            tmp_path / "specs",
            output,
            version=version,
            build_timestamp="2026-08-02T07:08:10+00:00",
        )

    assert not output.exists()
    assert not (tmp_path / "escape.zip").exists()


def test_release_source_commit_fails_closed_when_git_does_not_return_full_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout="deadbeef\n"),
    )

    with pytest.raises(RuntimeError, match="full Git SHA"):
        get_git_sha()


def test_malformed_domain_aborts_release_instead_of_omitting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "widgets.json").write_text("{not valid JSON")
    (specs / "CHANGELOG.md").write_text("# Changelog\n")
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "validation_report.json").write_text(
        json.dumps({"summary": {}, "discrepancies": []})
    )
    monkeypatch.chdir(tmp_path)
    builder = ReleaseBuilder(
        specs,
        tmp_path / "output",
        version="2026.08.02-1",
        build_timestamp="2026-08-02T07:08:10+00:00",
    )

    with pytest.raises(ValueError, match="domain spec cannot be read"):
        builder.build()

    assert not builder.artifact_path().exists()
    assert not (tmp_path / "output" / "api-specs-v2026.08.02-1").exists()


def test_partial_zip_failure_leaves_no_candidate_or_temporary_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scripts.release.get_git_sha", lambda: "a" * 40)
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "widgets.json").write_text(json.dumps({"openapi": "3.0.0", "info": {}, "paths": {}}))
    (specs / "CHANGELOG.md").write_text("# Changelog\n")
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "validation_report.json").write_text(
        json.dumps({"summary": {}, "discrepancies": []})
    )
    monkeypatch.chdir(tmp_path)
    builder = ReleaseBuilder(
        specs,
        tmp_path / "output",
        version="2026.08.02-1",
        build_timestamp="2026-08-02T07:08:10+00:00",
    )
    builder.artifact_path().write_bytes(b"stale candidate")

    def fail_write(*_args, **_kwargs):
        raise OSError("simulated archive write failure")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail_write)
    with pytest.raises(OSError, match="simulated archive write failure"):
        builder.build()

    assert not builder.artifact_path().exists()
    assert not list((tmp_path / "output").glob("*.tmp"))
