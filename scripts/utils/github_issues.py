"""Minimal httpx-based GitHub REST client for issue lifecycle."""

from __future__ import annotations

from typing import Any

import httpx


class GitHubIssues:
    """Thin client covering create / update / close / reopen / search-by-label."""

    def __init__(
        self,
        repo: str,
        token: str,
        base_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Build the REST client for ``repo`` authenticated with ``token``.

        ``transport`` is an optional httpx transport used to inject an
        offline fake during unit tests.
        """
        self._repo = repo
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=30.0,
            transport=transport,
        )

    def create(self, *, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        """Create a new issue and return the decoded JSON response."""
        r = self._client.post(
            f"/repos/{self._repo}/issues",
            json={"title": title, "body": body, "labels": labels},
        )
        r.raise_for_status()
        return r.json()

    def update(self, *, number: int, body: str) -> dict[str, Any]:
        """Replace the body of issue ``number`` and return the response JSON."""
        r = self._client.patch(
            f"/repos/{self._repo}/issues/{number}",
            json={"body": body},
        )
        r.raise_for_status()
        return r.json()

    def comment(self, *, number: int, body: str) -> dict[str, Any]:
        """Post a new comment on issue ``number`` and return the response JSON."""
        r = self._client.post(
            f"/repos/{self._repo}/issues/{number}/comments",
            json={"body": body},
        )
        r.raise_for_status()
        return r.json()

    def close(self, *, number: int, comment: str) -> None:
        """Add a closing comment on ``number`` and set its state to ``closed``."""
        self.comment(number=number, body=comment)
        r = self._client.patch(
            f"/repos/{self._repo}/issues/{number}",
            json={"state": "closed"},
        )
        r.raise_for_status()

    def reopen(self, *, number: int, comment: str) -> None:
        """Add a re-opening comment on ``number`` and set its state to ``open``."""
        self.comment(number=number, body=comment)
        r = self._client.patch(
            f"/repos/{self._repo}/issues/{number}",
            json={"state": "open"},
        )
        r.raise_for_status()

    def search_by_label(
        self, label: str, *, state: str = "open"
    ) -> list[dict[str, Any]]:
        """Return the list of issues carrying ``label`` in the given ``state``."""
        r = self._client.get(
            f"/repos/{self._repo}/issues",
            params={"labels": label, "state": state, "per_page": 100},
        )
        r.raise_for_status()
        return r.json()
