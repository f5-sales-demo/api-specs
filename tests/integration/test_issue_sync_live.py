"""Opt-in live-API smoke test for scripts.issue_sync.

Runs only when:
  - RUN_LIVE_INTEGRATION=1, AND
  - F5XC_API_TOKEN is set, AND
  - GITHUB_TOKEN is set.

Uses dry-run mode so the test never mutates GitHub state. The real live-API
re-probe IS exercised -- the test verifies the CLI writes a sensible mapping
JSON even when the reconcile-emitted discrepancy is synthetic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.issue_sync import main

_OPT_IN_ENV = "RUN_LIVE_INTEGRATION"


def _live_integration_enabled() -> bool:
    return (
        os.environ.get(_OPT_IN_ENV) == "1"
        and bool(os.environ.get("F5XC_API_TOKEN"))
        and bool(os.environ.get("GITHUB_TOKEN"))
    )


@pytest.mark.skipif(
    not _live_integration_enabled(),
    reason=(
        "Live integration test skipped. To run, export "
        "RUN_LIVE_INTEGRATION=1, F5XC_API_URL, F5XC_API_TOKEN, GITHUB_TOKEN, "
        "and GITHUB_REPOSITORY."
    ),
)
def test_issue_sync_dry_run_against_live_staging(tmp_path: Path) -> None:
    """Dry-run issue_sync against the Staging tenant; verify the mapping shape."""
    report = tmp_path / "validation_report.json"
    report.write_text(
        json.dumps(
            {
                "discrepancies": [
                    {
                        "path": "/public/namespaces/system/healthcheck/readiness",
                        "property_name": "unused",
                        "constraint_type": "minimum",
                        "discrepancy_type": "spec_stricter",
                        "domain": "healthcheck",
                        "method": "GET",
                        "spec_value": 1,
                        "api_behavior": {},
                        "test_values": [0],
                    },
                ],
            },
        ),
    )
    mapping_out = tmp_path / "issue_mapping.json"
    rc = main(
        [
            "--report",
            str(report),
            "--mapping-out",
            str(mapping_out),
            "--config",
            "config/issue_sync.yaml",
            "--dry-run",
            "--run-url",
            "https://example/run/live-smoke",
        ],
    )
    assert rc == 0, f"CLI should succeed in dry-run; rc={rc}"

    data = json.loads(mapping_out.read_text())
    assert data, "Expected at least one mapping entry for the synthetic discrepancy."
    for key, entry in data.items():
        action = entry["action"]
        assert action.startswith("dry-run-") or action in {
            "close-withheld-pending-reprobe",
            "skipped-close",
        }, f"Unexpected action for {key!r}: {action}"
