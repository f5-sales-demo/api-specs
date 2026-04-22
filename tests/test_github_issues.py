import json

import httpx
import pytest

from scripts.utils.github_issues import GitHubIssues


@pytest.fixture
def mock_transport():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        method = request.method
        path = request.url.path

        # Precise method+path dispatch — no substring matches that can
        # accidentally bleed across routes when new tests are added.
        if method == "POST" and path == "/repos/owner/repo/issues":
            return httpx.Response(201, json={"number": 42, "html_url": "https://x/42"})
        if method == "GET" and path == "/repos/owner/repo/issues":
            return httpx.Response(200, json=[])
        if method == "PATCH" and path == "/repos/owner/repo/issues/42":
            return httpx.Response(200, json={"number": 42, "html_url": "https://x/42"})
        if method == "POST" and path == "/repos/owner/repo/issues/42/comments":
            return httpx.Response(201, json={"id": 1})

        # Explicit failure so a bad route never silently passes.
        msg = f"Unmocked route: {method} {path}"
        raise AssertionError(msg)

    return httpx.MockTransport(handler), calls


def test_create_issue_posts_correct_payload(mock_transport):
    transport, calls = mock_transport
    client = GitHubIssues("owner/repo", token="t", transport=transport)  # noqa: S106
    result = client.create(title="T", body="B", labels=["x", "y"])
    assert result["number"] == 42
    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert calls[0].url.path == "/repos/owner/repo/issues"
    assert calls[0].headers["Authorization"] == "Bearer t"
    assert json.loads(calls[0].content) == {
        "title": "T",
        "body": "B",
        "labels": ["x", "y"],
    }


def test_update_issue_patches_body(mock_transport):
    transport, calls = mock_transport
    client = GitHubIssues("owner/repo", token="t", transport=transport)  # noqa: S106
    client.update(number=42, body="updated")
    assert len(calls) == 1
    assert calls[0].method == "PATCH"
    assert calls[0].url.path == "/repos/owner/repo/issues/42"
    assert json.loads(calls[0].content) == {"body": "updated"}


def test_close_issue_comments_then_sets_state_closed(mock_transport):
    transport, calls = mock_transport
    client = GitHubIssues("owner/repo", token="t", transport=transport)  # noqa: S106
    client.close(number=42, comment="done")
    assert len(calls) == 2
    # Comment first so the closing note persists even if the PATCH fails.
    assert calls[0].method == "POST"
    assert calls[0].url.path == "/repos/owner/repo/issues/42/comments"
    assert json.loads(calls[0].content) == {"body": "done"}
    assert calls[1].method == "PATCH"
    assert calls[1].url.path == "/repos/owner/repo/issues/42"
    assert json.loads(calls[1].content) == {"state": "closed"}


def test_reopen_issue_comments_then_sets_state_open(mock_transport):
    transport, calls = mock_transport
    client = GitHubIssues("owner/repo", token="t", transport=transport)  # noqa: S106
    client.reopen(number=42, comment="reappeared")
    assert len(calls) == 2
    assert calls[0].method == "POST"
    assert calls[0].url.path == "/repos/owner/repo/issues/42/comments"
    assert json.loads(calls[0].content) == {"body": "reappeared"}
    assert calls[1].method == "PATCH"
    assert calls[1].url.path == "/repos/owner/repo/issues/42"
    assert json.loads(calls[1].content) == {"state": "open"}


def test_search_by_label_sends_expected_query_params(mock_transport):
    transport, calls = mock_transport
    client = GitHubIssues("owner/repo", token="t", transport=transport)  # noqa: S106
    issues = client.search_by_label("disc:abcd1234", state="all")
    assert issues == []
    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert calls[0].url.path == "/repos/owner/repo/issues"
    params = dict(calls[0].url.params)
    assert params == {
        "labels": "disc:abcd1234",
        "state": "all",
        "per_page": "100",
    }
