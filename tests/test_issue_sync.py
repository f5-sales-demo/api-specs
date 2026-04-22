from unittest.mock import MagicMock

from scripts.issue_sync import (
    SyncPlan,
    compute_plan,
    render_issue_body,
    sync_discrepancies,
)
from scripts.utils.constraint_validator import Discrepancy, DiscrepancyType
from scripts.utils.discrepancy_fingerprint import fingerprint as compute_fp
from scripts.utils.discrepancy_reprobe import ReprobeEvidence


def _issue(num, fp, state="open"):
    """Construct a GitHub-issue-shaped dict with a disc:<fp> label."""
    return {
        "number": num,
        "state": state,
        "labels": [{"name": f"disc:{fp}"}],
    }


def test_plan_creates_for_new_fingerprint():
    existing: list[dict] = []
    current: dict[str, dict] = {
        "abcd1234ffffffffffffffffffffffffffffffff": {"fake": "disc"}
    }
    plan = compute_plan(existing, current)
    assert isinstance(plan, SyncPlan)
    assert plan.to_create == ["abcd1234ffffffffffffffffffffffffffffffff"]
    assert not plan.to_update
    assert not plan.to_close
    assert not plan.to_reopen
    assert not plan.skipped_close


def test_plan_updates_for_existing_open():
    fp = "abcd1234" + "f" * 32
    existing = [_issue(7, fp, "open")]
    current = {fp: {"fake": "disc"}}
    plan = compute_plan(existing, current)
    assert plan.to_update == [(7, fp)]
    assert not plan.to_create
    assert not plan.to_close
    assert not plan.to_reopen


def test_plan_closes_when_fingerprint_disappears():
    fp = "abcd1234" + "f" * 32
    existing = [_issue(7, fp, "open")]
    current: dict[str, dict] = {}
    plan = compute_plan(existing, current)
    assert plan.to_close == [(7, fp)]
    assert not plan.to_create
    assert not plan.to_update
    assert not plan.to_reopen


def test_plan_reopens_for_closed_fingerprint_that_reappears():
    fp = "abcd1234" + "f" * 32
    existing = [_issue(7, fp, "closed")]
    current = {fp: {"fake": "disc"}}
    plan = compute_plan(existing, current)
    assert plan.to_reopen == [(7, fp)]
    assert not plan.to_create
    assert not plan.to_update
    assert not plan.to_close


def test_plan_respects_do_not_auto_close_label():
    fp = "abcd1234" + "f" * 32
    issue = _issue(7, fp, "open")
    issue["labels"].append({"name": "do-not-auto-close"})
    existing = [issue]
    current: dict[str, dict] = {}
    plan = compute_plan(existing, current)
    assert not plan.to_close
    assert plan.skipped_close == [(7, fp)]


def test_plan_ignores_issues_without_disc_label():
    """An issue without any disc:<fp> label is not a tracked discrepancy.

    It must not appear in any SyncPlan bucket even if its state would
    otherwise make it a close/reopen candidate.
    """
    existing = [
        {"number": 99, "state": "open", "labels": [{"name": "do-not-auto-close"}]},
        {"number": 100, "state": "open", "labels": []},
    ]
    current: dict[str, dict] = {}
    plan = compute_plan(existing, current)
    assert not plan.to_create
    assert not plan.to_update
    assert not plan.to_close
    assert not plan.to_reopen
    assert not plan.skipped_close


def test_plan_mixed_batch_produces_multiple_buckets():
    """A single call with a mixed existing+current should populate several buckets."""
    fp_update = "a" * 40
    fp_close = "b" * 40
    fp_reopen = "c" * 40
    fp_create = "d" * 40
    existing = [
        _issue(1, fp_update, "open"),
        _issue(2, fp_close, "open"),
        _issue(3, fp_reopen, "closed"),
    ]
    current: dict[str, dict] = {
        fp_update: {},
        fp_reopen: {},
        fp_create: {},
    }
    plan = compute_plan(existing, current)
    assert plan.to_update == [(1, fp_update)]
    assert plan.to_close == [(2, fp_close)]
    assert plan.to_reopen == [(3, fp_reopen)]
    assert plan.to_create == [fp_create]
    assert not plan.skipped_close


def _d(path="/x", prop="p", dtype=DiscrepancyType.SPEC_STRICTER):
    return Discrepancy(
        path=path,
        property_name=prop,
        constraint_type="minLength",
        discrepancy_type=dtype,
        spec_value=1,
        api_behavior={},
        test_values=[0],
    )


def _evidence(status=400, still=True):
    return ReprobeEvidence(
        endpoint_url="https://t/x",
        method="POST",
        test_value=0,
        status_code=status,
        body_snippet="evidence",
        timestamp_utc="2026-04-21T00:00:00Z",
        discrepancy_still_present=still,
    )


def test_render_issue_body_includes_fingerprint_and_evidence():
    body = render_issue_body(
        fingerprint="abc" + "f" * 37,
        domain="origin_pool",
        method="POST",
        discrepancy=_d(),
        evidence=_evidence(),
        run_url="https://gh/run/1",
    )
    assert "<!-- discrepancy-id: abc" in body
    assert "origin_pool" in body
    assert "status_code: 400" in body
    assert "https://gh/run/1" in body


def test_sync_creates_issues_for_new_discrepancies():
    gh = MagicMock()
    gh.search_by_label.return_value = []
    gh.create.return_value = {"number": 1, "html_url": "u"}
    reprobe = MagicMock(return_value=_evidence())
    d = _d()
    fp_value = compute_fp(d, "origin_pool", "POST")
    mapping = sync_discrepancies(
        discrepancies=[(d, "origin_pool", "POST")],
        gh=gh,
        reprobe=reprobe,
        run_url="https://gh/run/1",
        dry_run=False,
    )
    assert len(mapping) == 1
    assert mapping[fp_value]["action"] == "created"
    assert mapping[fp_value]["issue_number"] == 1
    assert mapping[fp_value]["issue_url"] == "u"
    # Verify the create call carries the load-bearing contracts the downstream
    # workflow relies on: the [upstream] title prefix, the full-fingerprint
    # disc: label, the upstream-discrepancy marker label, and the fingerprint
    # HTML comment inside the body.
    gh.create.assert_called_once()
    kwargs = gh.create.call_args.kwargs
    assert kwargs["title"].startswith("[upstream] ")
    assert f"disc:{fp_value}" in kwargs["labels"]
    assert "upstream-discrepancy" in kwargs["labels"]
    assert f"<!-- discrepancy-id: {fp_value} -->" in kwargs["body"]


def test_sync_closes_issue_only_when_reprobe_confirms_resolution():
    """Close candidate (issue exists, not in current) should close only if re-probe says gone."""
    fp = "abcd1234" + "f" * 36
    # No current discrepancies -> the existing issue is a close candidate.
    existing_issue = {
        "number": 7,
        "state": "open",
        "labels": [{"name": f"disc:{fp}"}],
    }
    gh = MagicMock()
    gh.search_by_label.return_value = [existing_issue]
    # For closes, sync_discrepancies must build a synthetic Discrepancy from
    # the issue body's HTML comment to re-probe. Simplify by having the
    # caller opt out of re-probing for issues lacking recoverable context;
    # the function should leave a comment and skip closing.
    reprobe = MagicMock(return_value=_evidence(still=True))
    sync_discrepancies(
        discrepancies=[],
        gh=gh,
        reprobe=reprobe,
        run_url="https://gh/run/1",
        dry_run=False,
    )
    # Should NOT close; should leave a comment.
    gh.close.assert_not_called()
    gh.comment.assert_called()


def test_sync_dry_run_makes_no_api_writes():
    gh = MagicMock()
    gh.search_by_label.return_value = []
    reprobe = MagicMock(return_value=_evidence())
    d = _d()
    fp_value = compute_fp(d, "origin_pool", "POST")
    mapping = sync_discrepancies(
        discrepancies=[(d, "origin_pool", "POST")],
        gh=gh,
        reprobe=reprobe,
        run_url="u",
        dry_run=True,
    )
    # Mapping action is explicitly the dry-run variant so a regression that
    # records "created" while still skipping the API call is caught.
    assert mapping[fp_value]["action"] == "dry-run-created"
    # Read-side is fine in dry-run; write-side is not.
    gh.search_by_label.assert_called_once()
    gh.create.assert_not_called()
    gh.update.assert_not_called()
    gh.reopen.assert_not_called()
    gh.comment.assert_not_called()
    # Re-probe IS called in dry-run so evidence is real.
    reprobe.assert_called_once()


def test_sync_updates_existing_open_issue_on_match():
    d = _d()
    fp_value = compute_fp(d, "origin_pool", "POST")
    existing_issue = {
        "number": 5,
        "state": "open",
        "html_url": "https://x/5",
        "labels": [{"name": f"disc:{fp_value}"}],
    }
    gh = MagicMock()
    gh.search_by_label.return_value = [existing_issue]
    reprobe = MagicMock(return_value=_evidence())
    mapping = sync_discrepancies(
        discrepancies=[(d, "origin_pool", "POST")],
        gh=gh,
        reprobe=reprobe,
        run_url="u",
        dry_run=False,
    )
    gh.create.assert_not_called()
    gh.update.assert_called_once()
    assert mapping[fp_value]["action"] == "updated"
    assert mapping[fp_value]["issue_number"] == 5


def test_sync_reopens_existing_closed_issue_whose_fingerprint_reappeared():
    d = _d()
    fp_value = compute_fp(d, "origin_pool", "POST")
    existing_issue = {
        "number": 9,
        "state": "closed",
        "html_url": "https://x/9",
        "labels": [{"name": f"disc:{fp_value}"}],
    }
    gh = MagicMock()
    gh.search_by_label.return_value = [existing_issue]
    reprobe = MagicMock(return_value=_evidence())
    mapping = sync_discrepancies(
        discrepancies=[(d, "origin_pool", "POST")],
        gh=gh,
        reprobe=reprobe,
        run_url="u",
        dry_run=False,
    )
    gh.create.assert_not_called()
    gh.update.assert_not_called()
    gh.reopen.assert_called_once()
    # Reopen comment embeds the re-appearance marker + the full evidence body.
    reopen_kwargs = gh.reopen.call_args.kwargs
    assert reopen_kwargs["number"] == 9
    assert reopen_kwargs["comment"].startswith("Discrepancy reappeared.")
    assert f"<!-- discrepancy-id: {fp_value} -->" in reopen_kwargs["comment"]
    assert mapping[fp_value]["action"] == "reopened"
    assert mapping[fp_value]["issue_number"] == 9
    assert mapping[fp_value]["issue_url"] == "https://x/9"


def test_sync_dry_run_still_previews_close_candidates():
    """Dry-run must still surface close candidates so operators can preview them."""
    existing_issue = {
        "number": 11,
        "state": "open",
        "html_url": "https://x/11",
        "labels": [{"name": "disc:abcd1234" + "f" * 32}],
    }
    gh = MagicMock()
    gh.search_by_label.return_value = [existing_issue]
    reprobe = MagicMock(return_value=_evidence())
    mapping = sync_discrepancies(
        discrepancies=[],
        gh=gh,
        reprobe=reprobe,
        run_url="u",
        dry_run=True,
    )
    # Close candidate surfaces in the mapping even in dry-run, and no
    # write-side API call is made.
    key = "withheld-close:abcd1234" + "f" * 32
    assert mapping[key]["action"] == "close-withheld-pending-reprobe"
    assert mapping[key]["issue_number"] == 11
    assert mapping[key]["issue_url"] == "https://x/11"
    gh.comment.assert_not_called()
