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
ASSET_BYTES = b""


def _zip_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("openapi.json", '{"openapi":"3.0.3"}\n')
    return output.getvalue()


ASSET_BYTES = _zip_bytes()
ASSET_DIGEST = f"sha256:{hashlib.sha256(ASSET_BYTES).hexdigest()}"
EXPECTED_COMMIT = "a" * 40


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
    verify_asset_bytes(ASSET_BYTES, ASSET_DIGEST)


def test_rejects_downloaded_bytes_that_do_not_match_the_reported_digest():
    with pytest.raises(ReleaseVerificationError, match="digest does not match"):
        verify_asset_bytes(ASSET_BYTES + b"changed", ASSET_DIGEST)


def test_rejects_non_zip_bytes_even_when_the_digest_matches():
    content = b"not a zip"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"

    with pytest.raises(ReleaseVerificationError, match="valid ZIP"):
        verify_asset_bytes(content, digest)


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


def test_network_verification_retries_the_complete_contract_after_download_failure(monkeypatch):
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
        REPOSITORY,
        TAG,
        ASSET_NAME,
        ASSET_DIGEST,
        EXPECTED_COMMIT,
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
        REPOSITORY,
        TAG,
        ASSET_NAME,
        ASSET_DIGEST,
        EXPECTED_COMMIT,
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
            REPOSITORY,
            TAG,
            ASSET_NAME,
            ASSET_DIGEST,
            EXPECTED_COMMIT,
            retry=ReleaseVerificationRetry(attempts=1, interval_seconds=0),
            receipt_output=output,
        )

    assert not output.exists()


def test_network_verification_fails_closed_after_bounded_retries(monkeypatch):
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
            REPOSITORY,
            TAG,
            ASSET_NAME,
            ASSET_DIGEST,
            EXPECTED_COMMIT,
            retry=ReleaseVerificationRetry(attempts=2, interval_seconds=0),
        )
