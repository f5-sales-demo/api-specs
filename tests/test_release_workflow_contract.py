"""Structural guards for immutable release publication."""

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "validate-and-release.yml"


def _jobs():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def test_release_is_verified_after_publication():
    release_steps = _jobs()["release"]["steps"]
    setting_index = next(
        index
        for index, step in enumerate(release_steps)
        if step["name"] == "Require immutable releases setting"
    )
    create_index = next(
        index for index, step in enumerate(release_steps) if step["name"] == "Create GitHub Release"
    )
    verify_index = next(
        index
        for index, step in enumerate(release_steps)
        if step["name"] == "Verify immutable GitHub release"
    )

    assert setting_index < create_index < verify_index
    setting_command = release_steps[setting_index]["run"]
    assert "immutable-releases" in setting_command
    assert ".enabled == true" in setting_command
    create_command = release_steps[create_index]["run"]
    assert '--target "${GITHUB_SHA}"' in create_command
    command = release_steps[verify_index]["run"]
    assert "python -m scripts.verify_release" in command
    assert '--repository "${GITHUB_REPOSITORY}"' in command
    assert '--tag "v${VERSION}"' in command
    assert '--expected-asset "api-specs-v${VERSION}.zip"' in command
    assert '--local-asset "release/api-specs-v${VERSION}.zip"' in command
    assert '--expected-commit "${GITHUB_SHA}"' in command
    assert '--receipt-output "$RUNNER_TEMP/api-specs-release-receipt.json"' in command
    assert 'gh release verify "v${VERSION}"' in command
    assert 'gh release verify-asset "v${VERSION}"' in command
    assert "attestation_verified" in command


def test_downstream_dispatch_requires_the_verified_release_job_to_succeed():
    notify = _jobs()["notify-downstream"]

    assert "release" in notify["needs"]
    assert "needs.release.result == 'success'" in notify["if"]

    release = _jobs()["release"]
    assert release["outputs"]["release_receipt"] == (
        "${{ steps.verify_release.outputs.release_receipt }}"
    )
    dispatch = next(
        step
        for step in notify["steps"]
        if step["name"] == "Dispatch upstream-specs-released to api-specs-enriched"
    )
    assert dispatch["env"]["RELEASE_RECEIPT"] == "${{ needs.release.outputs.release_receipt }}"
    command = dispatch["run"]
    assert '--argjson receipt "$RELEASE_RECEIPT"' in command
    assert "release_receipt: $receipt" in command
    assert '--input "$RUNNER_TEMP/downstream-dispatch.json"' in command
    for legacy in (
        "client_payload[version]",
        "client_payload[release_tag]",
        "client_payload[release_url]",
        "client_payload[run_id]",
    ):
        assert legacy not in command


def test_semantic_decision_builds_and_compares_the_exact_release_candidate():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert "force_release" not in workflow[True]["workflow_dispatch"].get("inputs", {})

    check = _jobs()["check-release-needed"]
    steps = check["steps"]
    build_index = next(
        index for index, step in enumerate(steps) if step["name"] == "Build release candidate"
    )
    compare_index = next(
        index for index, step in enumerate(steps) if step["name"] == "Compare semantic release"
    )
    upload_index = next(
        index for index, step in enumerate(steps) if step["name"] == "Upload release candidate"
    )
    assert build_index < compare_index < upload_index

    compare = steps[compare_index]
    assert "python -m scripts.semantic_release compare" in compare["run"]
    assert "--current-archive" in compare["run"]
    assert "--candidate-version" in compare["run"]
    assert '--source-commit "${GITHUB_SHA}"' in compare["run"]
    assert "--recovery-directory release" in compare["run"]
    assert "force" not in compare["run"].lower()

    upload = steps[upload_index]
    assert upload["if"] == "steps.check.outputs.should_publish == 'true'"
    assert upload["with"]["path"] == "release/${{ steps.check.outputs.release_asset }}"

    release_steps = _jobs()["release"]["steps"]
    assert any(step["name"] == "Download release candidate" for step in release_steps)
    assert not any(step["name"] == "Build release package" for step in release_steps)


def test_release_recovery_verifies_and_dispatches_without_creating_a_new_release():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    check = _jobs()["check-release-needed"]
    assert check["outputs"]["should_publish"] == "${{ steps.check.outputs.should_publish }}"
    assert check["outputs"]["resume_publication"] == (
        "${{ steps.check.outputs.resume_publication }}"
    )

    release = _jobs()["release"]
    assert release["if"] == "needs.check-release-needed.outputs.should_publish == 'true'"
    steps = release["steps"]
    create = next(step for step in steps if step["name"] == "Create GitHub Release")
    verify = next(step for step in steps if step["name"] == "Verify immutable GitHub release")
    notes = next(step for step in steps if step["name"] == "Generate release notes")
    assert create["if"] == "needs.check-release-needed.outputs.semantic_changed == 'true'"
    assert notes["if"] == "needs.check-release-needed.outputs.semantic_changed == 'true'"
    assert "gh release view" not in create["run"]
    assert "if" not in verify

    notify = _jobs()["notify-downstream"]
    assert notify["if"] == "needs.release.result == 'success'"


def test_release_build_clock_comes_only_from_spec_timestamp():
    steps = _jobs()["check-release-needed"]["steps"]
    version = next(step for step in steps if step["name"] == "Determine version from metadata")
    build = next(step for step in steps if step["name"] == "Build release candidate")

    assert 'document.get("spec_timestamp")' in version["run"]
    assert "download_timestamp" not in version["run"]
    assert "build_timestamp=${BUILD_TIMESTAMP}" in version["run"]
    assert build["env"]["BUILD_TIMESTAMP"] == ("${{ steps.version.outputs.build_timestamp }}")
    assert '--build-timestamp "${BUILD_TIMESTAMP}"' in build["run"]


def test_release_notes_render_the_measured_semantic_decision():
    release_steps = _jobs()["release"]["steps"]
    notes = next(step for step in release_steps if step["name"] == "Generate release notes")
    command = notes["run"]

    assert "python -m scripts.semantic_release notes" in command
    assert "semantic-release-decision.json" in command
    assert "Code changes resulted in updated output" not in command
    assert "Upstream F5 XC specs updated" not in command


def test_live_validation_cannot_fall_back_or_ignore_failure():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    dispatch = workflow[True]["workflow_dispatch"]
    assert "skip_live_tests" not in dispatch.get("inputs", {})

    validate_steps = _jobs()["validate"]["steps"]
    assert not any(step["name"] == "Validate specs (dry run)" for step in validate_steps)
    live = next(step for step in validate_steps if step["name"] == "Validate specs (live)")
    command = live["run"]

    assert "if" not in live
    assert 'if [ -z "${F5XC_API_TOKEN}" ]' not in command
    assert "--dry-run" not in command
    assert "|| true" not in command
    assert "${F5XC_API_TOKEN:?" in command
    assert "${F5XC_API_URL:?" in command
    assert "${F5XC_NAMESPACE:?" in command
    assert live["env"]["F5XC_NAMESPACE"] == "${{ secrets.F5XC_NAMESPACE }}"


def test_failure_tracker_cannot_close_when_publication_recovery_was_skipped():
    tracker_steps = _jobs()["failure-tracker"]["steps"]
    reconcile = next(
        step
        for step in tracker_steps
        if step["name"] == "Reconcile the failure tracker with this run"
    )
    script = reconcile["with"]["script"]

    assert "publicationRecoverySkipped" in script
    assert "keeping the tracker open" in script


def test_every_direct_action_in_release_workflow_is_commit_pinned():
    action_refs = re.findall(r"^\s*uses:\s+([^\s@]+)@([^\s#]+)", WORKFLOW.read_text(), re.MULTILINE)
    assert action_refs
    for action, ref in action_refs:
        assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{action}@{ref} is mutable"
