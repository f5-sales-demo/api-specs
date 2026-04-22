"""Sync reconcile discrepancies to GitHub issues.

The module is split into two halves:

* Pure plan computation — :class:`SyncPlan` + :func:`compute_plan`. No I/O,
  no time, no randomness; deterministic diff of existing GitHub issues
  against the current reconcile findings.
* Impure execution — :func:`render_issue_body` + :func:`sync_discrepancies`.
  Drives the GitHub REST client and a live-API re-probe, rendering each
  tracked issue with fresh evidence.

CI invokes the execution half via the ``scripts.issue_sync`` module entry
point (Task A6); tests exercise both halves offline with injected
``GitHubIssues`` and ``reprobe`` callables.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .utils.constraint_validator import Discrepancy, DiscrepancyType
from .utils.discrepancy_fingerprint import fingerprint as _fingerprint
from .utils.discrepancy_reprobe import reprobe_discrepancy as _reprobe_discrepancy
from .utils.github_issues import GitHubIssues

if TYPE_CHECKING:
    from collections.abc import Callable

    from .utils.discrepancy_reprobe import ReprobeEvidence


@dataclass
class SyncPlan:
    """Diff between existing GitHub issues and current reconcile findings.

    Attributes:
        to_create: Fingerprints with no matching GitHub issue yet.
        to_update: ``(issue_number, fingerprint)`` pairs whose bodies need
            refreshing because the discrepancy still exists.
        to_close: ``(issue_number, fingerprint)`` pairs whose underlying
            discrepancy no longer appears in the current run and that do
            not carry the ``do-not-auto-close`` label.
        to_reopen: ``(issue_number, fingerprint)`` pairs for closed issues
            whose discrepancy has reappeared.
        skipped_close: ``(issue_number, fingerprint)`` pairs that would
            have been closed but are pinned open via ``do-not-auto-close``.
    """

    to_create: list[str] = field(default_factory=list)
    to_update: list[tuple[int, str]] = field(default_factory=list)
    to_close: list[tuple[int, str]] = field(default_factory=list)
    to_reopen: list[tuple[int, str]] = field(default_factory=list)
    skipped_close: list[tuple[int, str]] = field(default_factory=list)


def _labels_to_set(issue: dict[str, Any]) -> set[str]:
    """Return the label names attached to ``issue`` as a set."""
    return {lbl["name"] for lbl in issue.get("labels", [])}


def _fingerprint_from_labels(labels: set[str]) -> str | None:
    """Return the full 40-hex fingerprint carried by the ``disc:<fp>`` label.

    Each tracked issue carries exactly one ``disc:<40-hex>`` label holding
    the full fingerprint (GitHub labels allow up to 50 chars, so ``disc:``
    prefix + 40 hex fits comfortably). Returns ``None`` when the issue
    carries no such label.
    """
    for name in labels:
        if name.startswith("disc:"):
            return name[len("disc:") :]
    return None


def compute_plan(
    existing: list[dict[str, Any]],
    current: dict[str, Any],
) -> SyncPlan:
    """Diff ``existing`` GitHub issues against ``current`` discrepancies.

    Args:
        existing: Issues already on GitHub, each represented as a dict with
            at least ``number``, ``state``, and ``labels`` keys. Labels are
            expected in the GitHub REST shape ``[{"name": ...}, ...]`` and
            the discrepancy is identified by a ``disc:<40-hex>`` label.
        current: Mapping of full 40-hex fingerprint to a Discrepancy-like
            payload for every discrepancy detected in the current run.

    Returns:
        A :class:`SyncPlan` describing the create/update/close/reopen
        actions needed to bring GitHub in line with ``current``.
    """
    plan = SyncPlan()
    seen_fps: set[str] = set()

    for issue in existing:
        labels = _labels_to_set(issue)
        label_fp = _fingerprint_from_labels(labels)
        if label_fp is None:
            continue
        state = issue.get("state", "open")
        in_current = label_fp in current

        if in_current:
            seen_fps.add(label_fp)
            if state == "open":
                plan.to_update.append((issue["number"], label_fp))
            else:
                plan.to_reopen.append((issue["number"], label_fp))
        elif state == "open":
            # Open issue whose fingerprint is absent from the current run
            # is a candidate for auto-close, unless pinned.
            if "do-not-auto-close" in labels:
                plan.skipped_close.append((issue["number"], label_fp))
            else:
                plan.to_close.append((issue["number"], label_fp))

    for fp in current:
        if fp not in seen_fps:
            plan.to_create.append(fp)

    return plan


_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    keep_trailing_newline=True,
)


def render_issue_body(
    *,
    fingerprint: str,
    domain: str,
    method: str,
    discrepancy: Discrepancy,
    evidence: ReprobeEvidence,
    run_url: str,
) -> str:
    """Render the markdown body for an upstream-discrepancy tracking issue."""
    template = _env.get_template("issue_body.md.j2")
    return template.render(
        fingerprint=fingerprint,
        domain=domain,
        method=method,
        discrepancy=discrepancy,
        evidence=evidence,
        run_url=run_url,
    )


def _title(d: Discrepancy, domain: str, method: str) -> str:
    """Return the GitHub issue title for a tracked discrepancy."""
    return (
        f"[upstream] {d.discrepancy_type.value}: "
        f"{domain} {method.upper()} {d.path} — {d.property_name}"
    )


def _labels(d: Discrepancy, domain: str, fp: str) -> list[str]:
    """Return the label set applied to a new tracking issue."""
    return [
        "upstream-discrepancy",
        f"disc-type:{d.discrepancy_type.value.replace('_', '-')}",
        f"domain:{domain.replace('_', '-')}",
        f"disc:{fp}",  # FULL 40-hex fingerprint in the label
    ]


# pylint: disable=too-many-locals,too-many-branches
# The function mirrors the five-branch lifecycle defined in the design
# spec §3.4 (create / update / reopen / close-candidate-with-comment /
# skipped-close), so the local-variable and branch counts are load-bearing.
# Splitting would obscure that mapping.
def sync_discrepancies(
    *,
    discrepancies: list[tuple[Discrepancy, str, str]],
    gh: GitHubIssues,
    reprobe: Callable[[Discrepancy, str, str], ReprobeEvidence],
    run_url: str,
    dry_run: bool = False,
) -> dict[str, dict]:
    """Sync discrepancies to GitHub issues; return a fingerprint-to-action map.

    Creates/updates/reopens issues for each ``(discrepancy, domain, method)``
    tuple in ``discrepancies``. A fresh live re-probe is attached as
    evidence on every pass. Issues that exist on GitHub under the
    ``upstream-discrepancy`` label but are absent from ``discrepancies``
    are close candidates; each close candidate receives a comment noting
    its disappearance from the latest reconcile run and the actual close
    is deferred (this function never calls ``gh.close`` — a follow-up
    path that can re-probe with full discrepancy context owns that
    lifecycle, per spec section 3.4).

    Args:
        discrepancies: ``(discrepancy, domain, method)`` tuples for every
            discrepancy detected in the current reconcile run.
        gh: Thin GitHub REST client from ``github_issues.GitHubIssues``.
        reprobe: Callable that issues a live probe and returns
            :class:`ReprobeEvidence`. Invoked for every current
            discrepancy, including in dry-run so the rendered body
            contains real evidence.
        run_url: URL pointing at the detecting CI run; embedded in the
            issue body for traceability.
        dry_run: When True, no GitHub API write is issued (no create,
            update, reopen, or comment). The read-side search AND the
            re-probe ARE still called so the returned mapping previews
            exactly what a live run would do.

    Returns:
        A mapping from fingerprint (or ``withheld-close:<fp>`` /
        ``skip-close:<fp>`` synthetic keys) to an action dict with
        ``action``, ``issue_number``, and ``issue_url`` keys.
    """
    mapping: dict[str, dict] = {}

    # Read-side is always executed, even in dry-run, so the returned mapping
    # faithfully previews the full plan (including close candidates).
    existing = gh.search_by_label("upstream-discrepancy", state="all")
    existing_by_fp: dict[str, dict] = {}
    for i in existing:
        for lbl in i.get("labels", []):
            if lbl["name"].startswith("disc:"):
                existing_by_fp[lbl["name"][len("disc:") :]] = i
                break

    current_fps: set[str] = set()

    for d, domain, method in discrepancies:
        fp = _fingerprint(d, domain=domain, method=method)
        current_fps.add(fp)
        evidence = reprobe(d, domain, method)
        body = render_issue_body(
            fingerprint=fp,
            domain=domain,
            method=method,
            discrepancy=d,
            evidence=evidence,
            run_url=run_url,
        )
        existing_issue = existing_by_fp.get(fp)

        if existing_issue is None:
            if dry_run:
                mapping[fp] = {
                    "action": "dry-run-created",
                    "issue_number": None,
                    "issue_url": None,
                }
            else:
                result = gh.create(
                    title=_title(d, domain, method),
                    body=body,
                    labels=_labels(d, domain, fp),
                )
                mapping[fp] = {
                    "action": "created",
                    "issue_number": result["number"],
                    "issue_url": result["html_url"],
                }
        elif existing_issue["state"] == "open":
            if dry_run:
                mapping[fp] = {
                    "action": "dry-run-updated",
                    "issue_number": existing_issue["number"],
                    "issue_url": existing_issue.get("html_url"),
                }
            else:
                gh.update(number=existing_issue["number"], body=body)
                mapping[fp] = {
                    "action": "updated",
                    "issue_number": existing_issue["number"],
                    "issue_url": existing_issue.get("html_url"),
                }
        elif dry_run:
            mapping[fp] = {
                "action": "dry-run-reopened",
                "issue_number": existing_issue["number"],
                "issue_url": existing_issue.get("html_url"),
            }
        else:
            # Closed issue whose fingerprint reappeared — reopen it.
            gh.reopen(
                number=existing_issue["number"],
                comment=f"Discrepancy reappeared.\n\n{body}",
            )
            mapping[fp] = {
                "action": "reopened",
                "issue_number": existing_issue["number"],
                "issue_url": existing_issue.get("html_url"),
            }

    # Close candidates: existing open issues whose fingerprint is absent
    # from the current run. We cannot re-probe without the original
    # Discrepancy context, so leave an explanatory comment and defer.
    for fp, issue in existing_by_fp.items():
        if issue["state"] != "open":
            continue
        if fp in current_fps:
            continue
        labels = {lbl["name"] for lbl in issue.get("labels", [])}
        if "do-not-auto-close" in labels:
            if not dry_run:
                gh.comment(
                    number=issue["number"],
                    body="Skipped auto-close: do-not-auto-close label present.",
                )
            mapping[f"skip-close:{fp}"] = {
                "action": "skipped-close",
                "issue_number": issue["number"],
                "issue_url": issue.get("html_url"),
            }
            continue
        if not dry_run:
            gh.comment(
                number=issue["number"],
                body=(
                    "Fingerprint absent from latest reconcile run; "
                    f"close withheld pending live re-probe.\n\n{run_url}"
                ),
            )
        mapping[f"withheld-close:{fp}"] = {
            "action": "close-withheld-pending-reprobe",
            "issue_number": issue["number"],
            "issue_url": issue.get("html_url"),
        }

    return mapping


def main(argv: list[str] | None = None) -> int:
    """CLI entry point invoked by the validate-and-release workflow.

    Reads validation_report.json, runs sync_discrepancies, writes the
    resulting mapping to reports/issue_mapping.json (or the path specified
    by --mapping-out). Returns 0 on success, non-zero on failure.
    """
    parser = argparse.ArgumentParser(
        description="Sync upstream-spec discrepancies to GitHub issues.",
    )
    parser.add_argument(
        "--report",
        required=True,
        help="Path to reports/validation_report.json produced by reconcile.",
    )
    parser.add_argument(
        "--mapping-out",
        required=True,
        help="Path where the fingerprint->action mapping JSON is written.",
    )
    parser.add_argument("--config", default="config/issue_sync.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--run-url",
        default="",
        help="URL of the detecting CI run; embedded in issue body for traceability.",
    )
    args = parser.parse_args(argv)

    with Path(args.config).open() as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg.get("enabled", True):
        Path(args.mapping_out).write_text("{}\n")
        return 0

    report = json.loads(Path(args.report).read_text())
    discrepancies = _load_discrepancies(report.get("discrepancies", []))

    # Short-circuit when there is no work to do: skip GitHub/API reads entirely
    # and emit an empty mapping. This keeps dry-run invocations cheap and avoids
    # surfacing transient credential/network errors when the report is clean.
    if not discrepancies:
        Path(args.mapping_out).write_text("{}\n")
        return 0

    gh = GitHubIssues(
        repo=os.environ["GITHUB_REPOSITORY"],
        token=os.environ["GITHUB_TOKEN"],
    )

    client = httpx.Client(
        base_url=os.environ["F5XC_API_URL"],
        headers={"Authorization": f"Bearer {os.environ['F5XC_API_TOKEN']}"},
        timeout=30.0,
    )

    def reprobe(
        d: Discrepancy,
        domain: str,
        method: str,
    ) -> ReprobeEvidence:
        return _reprobe_discrepancy(d, domain, method, client=client)

    try:
        try:
            mapping = sync_discrepancies(
                discrepancies=discrepancies,
                gh=gh,
                reprobe=reprobe,
                run_url=args.run_url,
                dry_run=args.dry_run,
            )
        except (httpx.HTTPError, OSError) as exc:
            # Surface any live-API / GitHub error as a non-zero exit so CI
            # fails loudly instead of silently writing a partial mapping.
            print(f"issue_sync: sync failed: {exc}", file=sys.stderr)
            return 1
    finally:
        client.close()

    Path(args.mapping_out).write_text(
        json.dumps(mapping, indent=2, sort_keys=True) + "\n",
    )
    return 0


def _load_discrepancies(raw: list[dict]) -> list[tuple[Discrepancy, str, str]]:
    """Convert raw JSON discrepancies into (Discrepancy, domain, method) tuples."""
    out = []
    for entry in raw:
        d = Discrepancy(
            path=entry["path"],
            property_name=entry["property_name"],
            constraint_type=entry["constraint_type"],
            discrepancy_type=DiscrepancyType(entry["discrepancy_type"]),
            spec_value=entry.get("spec_value"),
            api_behavior=entry.get("api_behavior"),
            test_values=entry.get("test_values", []),
        )
        out.append((d, entry["domain"], entry["method"]))
    return out


if __name__ == "__main__":
    sys.exit(main())
