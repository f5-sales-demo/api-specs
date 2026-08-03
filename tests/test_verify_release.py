"""Tests for immutable GitHub release verification."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Callable
from typing import Any

import pytest
import requests

from scripts.verify_release import (
    ExpectedRelease,
    ReleaseVerificationError,
    ReleaseVerificationRetry,
    validate_release_metadata,
    validate_tag_ref,
    verify_asset_bytes,
    verify_published_release,
)

REPOSITORY = "f5-sales-demo/api-specs"
TAG = "v2026.08.01-1"
ASSET_NAME = f"api-specs-{TAG}.zip"
EXPECTED_COMMIT = "a" * 40


def _canonical_member(path: str) -> zipfile.ZipInfo:
    member = zipfile.ZipInfo(path, date_time=(2026, 8, 1, 11, 0, 0))
    member.compress_type = zipfile.ZIP_STORED
    member.create_system = 3
    member.external_attr = 0o100644 << 16
    member.internal_attr = 0
    member.extra = b""
    member.comment = b""
    return member


def _zip_bytes() -> bytes:
    version = TAG.removeprefix("v")
    aggregate = json.dumps(
        {
            "openapi": "3.0.0",
            "info": {"title": "Aggregate", "version": version},
            "paths": {},
            "components": {"schemas": {}},
        }
    ).encode()
    files = {
        "domains/widgets.json": json.dumps(
            {"openapi": "3.0.0", "info": {"title": "Widgets", "version": "1"}, "paths": {}}
        ).encode(),
        "openapi.json": aggregate,
        "openapi.yaml": aggregate,
        "CHANGELOG.md": b"# Changelog\n",
        "VALIDATION_REPORT.md": b"# Validation\n\n**Generated:** 2026-08-01T11:00:00+00:00\n",
    }
    manifest = {
        "schema_version": 1,
        "version": version,
        "generated_at": "2026-08-01T11:00:00+00:00",
        "git_sha": EXPECTED_COMMIT,
        "files": [
            {
                "path": path,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for path, content in sorted(files.items())
        ],
    }
    payloads = {**files, "manifest.json": json.dumps(manifest).encode()}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        for path, content in sorted(payloads.items()):
            archive.writestr(_canonical_member(path), content)
    return output.getvalue()


ASSET_BYTES = _zip_bytes()
ASSET_DIGEST = f"sha256:{hashlib.sha256(ASSET_BYTES).hexdigest()}"
EXPECTED_RELEASE = ExpectedRelease(
    repository=REPOSITORY,
    tag=TAG,
    asset_name=ASSET_NAME,
    asset_digest=ASSET_DIGEST,
    commit=EXPECTED_COMMIT,
)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"attempts": 0}, "attempts must be at least one"),
        ({"interval_seconds": -1}, "interval_seconds cannot be negative"),
    ],
)
def test_retry_policy_rejects_unbounded_or_invalid_waits(kwargs, message):
    with pytest.raises(ValueError, match=message):
        ReleaseVerificationRetry(**kwargs)


def _release(**overrides):
    release = {
        "tag_name": TAG,
        "draft": False,
        "prerelease": False,
        "immutable": True,
        "published_at": "2026-08-01T12:00:00Z",
        "assets": [
            {
                "name": ASSET_NAME,
                "state": "uploaded",
                "content_type": "application/zip",
                "size": len(ASSET_BYTES),
                "digest": ASSET_DIGEST,
                "browser_download_url": (
                    f"https://github.com/{REPOSITORY}/releases/download/{TAG}/{ASSET_NAME}"
                ),
            }
        ],
    }
    release.update(overrides)
    return release


def test_accepts_one_final_immutable_release_asset_with_sha256():
    asset = validate_release_metadata(_release(), REPOSITORY, TAG, ASSET_NAME, ASSET_DIGEST)

    assert asset["digest"] == ASSET_DIGEST


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("draft", True, "draft"),
        ("prerelease", True, "prerelease"),
        ("immutable", False, "not immutable"),
        ("immutable", None, "not immutable"),
        ("published_at", None, "publication timestamp"),
    ],
)
def test_rejects_a_release_that_is_not_final_and_immutable(field, value, message):
    with pytest.raises(ReleaseVerificationError, match=message):
        validate_release_metadata(
            _release(**{field: value}), REPOSITORY, TAG, ASSET_NAME, ASSET_DIGEST
        )


def test_rejects_a_different_release_tag():
    with pytest.raises(ReleaseVerificationError, match="tag"):
        validate_release_metadata(
            _release(tag_name="vwrong"), REPOSITORY, TAG, ASSET_NAME, ASSET_DIGEST
        )


@pytest.mark.parametrize("assets", [[], [_release()["assets"][0], _release()["assets"][0]]])
def test_rejects_any_asset_count_other_than_one(assets):
    with pytest.raises(ReleaseVerificationError, match="exactly one"):
        validate_release_metadata(
            _release(assets=assets), REPOSITORY, TAG, ASSET_NAME, ASSET_DIGEST
        )


@pytest.mark.parametrize(
    ("asset_update", "message"),
    [
        ({"name": "wrong.zip"}, "asset name"),
        ({"state": "new"}, "uploaded"),
        ({"content_type": "application/octet-stream"}, "content type"),
        ({"size": 0}, "size"),
        ({"size": True}, "size"),
        ({"digest": None}, "SHA-256 digest"),
        ({"digest": "sha256:not-a-digest"}, "SHA-256 digest"),
        ({"browser_download_url": "https://example.com/wrong.zip"}, "download URL"),
    ],
)
def test_rejects_an_asset_that_does_not_match_the_contract(asset_update, message):
    asset = {**_release()["assets"][0], **asset_update}

    with pytest.raises(ReleaseVerificationError, match=message):
        validate_release_metadata(
            _release(assets=[asset]), REPOSITORY, TAG, ASSET_NAME, ASSET_DIGEST
        )


def test_rejects_a_github_digest_that_differs_from_the_locally_built_zip():
    asset = {**_release()["assets"][0], "digest": "sha256:" + "b" * 64}

    with pytest.raises(ReleaseVerificationError, match="locally built asset"):
        validate_release_metadata(
            _release(assets=[asset]), REPOSITORY, TAG, ASSET_NAME, ASSET_DIGEST
        )


def test_tag_ref_must_resolve_directly_to_the_expected_commit():
    validate_tag_ref(
        {"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": EXPECTED_COMMIT}},
        TAG,
        EXPECTED_COMMIT,
    )


@pytest.mark.parametrize(
    "tag_ref",
    [
        {"ref": "refs/tags/wrong", "object": {"type": "commit", "sha": EXPECTED_COMMIT}},
        {"ref": f"refs/tags/{TAG}", "object": {"type": "tag", "sha": "b" * 40}},
        {"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": "b" * 40}},
    ],
)
def test_rejects_a_tag_that_does_not_directly_name_the_expected_commit(tag_ref):
    with pytest.raises(ReleaseVerificationError, match="tag ref"):
        validate_tag_ref(tag_ref, TAG, EXPECTED_COMMIT)


def test_downloaded_asset_must_match_github_sha256_and_be_a_valid_zip():
    verify_asset_bytes(
        ASSET_BYTES,
        ASSET_DIGEST,
        expected_version=TAG.removeprefix("v"),
        expected_commit=EXPECTED_COMMIT,
    )


def test_rejects_downloaded_bytes_that_do_not_match_the_reported_digest():
    with pytest.raises(ReleaseVerificationError, match="digest does not match"):
        verify_asset_bytes(
            ASSET_BYTES + b"changed",
            ASSET_DIGEST,
            expected_version=TAG.removeprefix("v"),
            expected_commit=EXPECTED_COMMIT,
        )


def test_rejects_non_zip_bytes_even_when_the_digest_matches():
    content = b"not a zip"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"

    with pytest.raises(ReleaseVerificationError, match="valid ZIP"):
        verify_asset_bytes(
            content,
            digest,
            expected_version=TAG.removeprefix("v"),
            expected_commit=EXPECTED_COMMIT,
        )


class _Response(requests.Response):
    def __init__(self, *, payload=None, content: bytes = b"", status_code: int = 200):
        super().__init__()
        self._payload = payload
        self._content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self, **_kwargs: Any) -> Any:
        return self._payload


def _tag_ref():
    return {"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": EXPECTED_COMMIT}}


def _get_sequence(items: list[object]) -> Callable[..., _Response]:
    sequence = iter(items)

    def fake_get(*_args, **_kwargs):
        item = next(sequence)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, _Response)
        return item

    return fake_get


def test_network_verification_retries_the_complete_contract_after_download_failure(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        requests,
        "get",
        _get_sequence(
            [
                _Response(payload=_tag_ref()),
                _Response(payload=_release()),
                requests.ConnectionError("transient"),
                _Response(payload=_tag_ref()),
                _Response(payload=_release()),
                _Response(content=ASSET_BYTES),
            ]
        ),
    )
    sleeps: list[float] = []
    monkeypatch.setattr("scripts.verify_release.time.sleep", sleeps.append)

    digest = verify_published_release(
        EXPECTED_RELEASE,
        install_dir=tmp_path / "installed",
        retry=ReleaseVerificationRetry(attempts=2, interval_seconds=0.25),
    )

    assert digest == ASSET_DIGEST
    assert sleeps == [0.25]


def test_verified_download_writes_exact_downstream_receipt(monkeypatch, tmp_path):
    monkeypatch.setattr(
        requests,
        "get",
        _get_sequence(
            [
                _Response(payload=_tag_ref()),
                _Response(payload=_release()),
                _Response(content=ASSET_BYTES),
            ]
        ),
    )
    output = tmp_path / "receipt.json"

    verify_published_release(
        EXPECTED_RELEASE,
        install_dir=tmp_path / "installed",
        retry=ReleaseVerificationRetry(attempts=1, interval_seconds=0),
        receipt_output=output,
    )

    receipt = json.loads(output.read_text())
    assert receipt == {
        "version": TAG.removeprefix("v"),
        "tag_name": TAG,
        "published_at": "2026-08-01T12:00:00Z",
        "asset_name": ASSET_NAME,
        "asset_size": len(ASSET_BYTES),
        "asset_digest": ASSET_DIGEST,
    }


def test_verified_download_installs_the_exact_public_asset(monkeypatch, tmp_path):
    monkeypatch.setattr(
        requests,
        "get",
        _get_sequence(
            [
                _Response(payload=_tag_ref()),
                _Response(payload=_release()),
                _Response(content=ASSET_BYTES),
            ]
        ),
    )
    target = tmp_path / "installed"

    verify_published_release(
        EXPECTED_RELEASE,
        retry=ReleaseVerificationRetry(attempts=1, interval_seconds=0),
        install_dir=target,
    )

    assert (target / "manifest.json").is_file()
    assert (target / "openapi.json").is_file()
    assert list((target / "domains").glob("*.json"))


def test_install_failure_is_not_retried_or_emitted_as_a_receipt(monkeypatch, tmp_path):
    monkeypatch.setattr(
        requests,
        "get",
        _get_sequence(
            [
                _Response(payload=_tag_ref()),
                _Response(payload=_release()),
                _Response(content=ASSET_BYTES),
            ]
        ),
    )
    target = tmp_path / "installed"
    target.mkdir()
    receipt = tmp_path / "receipt.json"

    with pytest.raises(ReleaseVerificationError, match="already exists"):
        verify_published_release(
            EXPECTED_RELEASE,
            retry=ReleaseVerificationRetry(attempts=2, interval_seconds=0),
            install_dir=target,
            receipt_output=receipt,
        )

    assert not receipt.exists()


def test_receipt_output_never_follows_or_overwrites_a_symlink(monkeypatch, tmp_path):
    monkeypatch.setattr(
        requests,
        "get",
        _get_sequence(
            [
                _Response(payload=_tag_ref()),
                _Response(payload=_release()),
                _Response(content=ASSET_BYTES),
            ]
        ),
    )
    owner_data = tmp_path / "owner-data"
    owner_data.write_text("preserve\n", encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    receipt.symlink_to(owner_data)

    with pytest.raises(ReleaseVerificationError, match="already exists|unsafe"):
        verify_published_release(
            EXPECTED_RELEASE,
            retry=ReleaseVerificationRetry(attempts=1, interval_seconds=0),
            install_dir=tmp_path / "installed",
            receipt_output=receipt,
        )

    assert owner_data.read_text(encoding="utf-8") == "preserve\n"


def test_verified_download_rejects_release_metadata_size_that_does_not_match_bytes(
    monkeypatch, tmp_path
):
    release = _release()
    release["assets"][0]["size"] = len(ASSET_BYTES) + 1
    monkeypatch.setattr(
        requests,
        "get",
        _get_sequence(
            [
                _Response(payload=_tag_ref()),
                _Response(payload=release),
                _Response(content=ASSET_BYTES),
            ]
        ),
    )
    output = tmp_path / "receipt.json"

    with pytest.raises(ReleaseVerificationError, match="size"):
        verify_published_release(
            EXPECTED_RELEASE,
            install_dir=tmp_path / "installed",
            retry=ReleaseVerificationRetry(attempts=1, interval_seconds=0),
            receipt_output=output,
        )

    assert not output.exists()


def test_verified_download_rejects_malformed_manifest_before_writing_receipt(
    monkeypatch,
    tmp_path,
):
    with zipfile.ZipFile(io.BytesIO(ASSET_BYTES)) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(entries["manifest.json"])
    manifest["files"][0]["sha256"] = "0" * 64
    entries["manifest.json"] = json.dumps(manifest).encode()
    output_bytes = io.BytesIO()
    with zipfile.ZipFile(output_bytes, "w", zipfile.ZIP_STORED) as archive:
        for name, content in sorted(entries.items()):
            archive.writestr(_canonical_member(name), content)
    malformed = output_bytes.getvalue()
    malformed_digest = f"sha256:{hashlib.sha256(malformed).hexdigest()}"
    release = _release()
    release["assets"][0].update(size=len(malformed), digest=malformed_digest)
    expected = ExpectedRelease(
        repository=REPOSITORY,
        tag=TAG,
        asset_name=ASSET_NAME,
        asset_digest=malformed_digest,
        commit=EXPECTED_COMMIT,
    )
    monkeypatch.setattr(
        requests,
        "get",
        _get_sequence(
            [
                _Response(payload=_tag_ref()),
                _Response(payload=release),
                _Response(content=malformed),
            ]
        ),
    )
    receipt = tmp_path / "receipt.json"

    with pytest.raises(ReleaseVerificationError, match="manifest|digest"):
        verify_published_release(
            expected,
            install_dir=tmp_path / "installed",
            retry=ReleaseVerificationRetry(attempts=1, interval_seconds=0),
            receipt_output=receipt,
        )

    assert not receipt.exists()


def test_network_verification_fails_closed_after_bounded_retries(monkeypatch, tmp_path):
    mutable = _release(immutable=False)
    monkeypatch.setattr(
        requests,
        "get",
        _get_sequence(
            [
                _Response(payload=_tag_ref()),
                _Response(payload=mutable),
                _Response(payload=_tag_ref()),
                _Response(payload=mutable),
            ]
        ),
    )
    monkeypatch.setattr("scripts.verify_release.time.sleep", lambda _seconds: None)

    with pytest.raises(ReleaseVerificationError, match="not immutable"):
        verify_published_release(
            EXPECTED_RELEASE,
            install_dir=tmp_path / "installed",
            retry=ReleaseVerificationRetry(attempts=2, interval_seconds=0),
        )
