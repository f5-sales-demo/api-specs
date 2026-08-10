"""Structural guards for immutable release publication."""

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "validate-and-release.yml"
LOCKFILE = Path(__file__).parents[1] / "uv.lock"
TEST_WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "tests.yml"


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
    assert '--expected-commit "${RELEASE_COMMIT}"' in command
    assert '--receipt-output "$RUNNER_TEMP/api-specs-release-receipt.json"' in command
    assert '--install-dir "$RUNNER_TEMP/api-specs-install-uat"' in command
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

    jobs = _jobs()
    metadata = jobs["release-metadata"]
    builder = jobs["build-release-candidate"]
    check = jobs["check-release-needed"]
    assert metadata["needs"] == "validate"
    assert builder["needs"] == ["validate", "release-metadata"]
    assert builder["strategy"]["matrix"]["candidate"] == ["one", "two"]
    assert check["needs"] == [
        "validate",
        "release-metadata",
        "build-release-candidate",
    ]

    build = next(step for step in builder["steps"] if step["name"] == "Build release candidate")
    assert build["run"].count("python -m scripts.release") == 1
    built_upload = next(
        step for step in builder["steps"] if step["name"] == "Upload release candidate"
    )
    assert built_upload["with"]["name"] == "release-candidate-${{ matrix.candidate }}"

    steps = check["steps"]
    compare_bytes_index = next(
        index
        for index, step in enumerate(steps)
        if step["name"] == "Compare independent release candidates"
    )
    compare_index = next(
        index for index, step in enumerate(steps) if step["name"] == "Compare semantic release"
    )
    upload_index = next(
        index for index, step in enumerate(steps) if step["name"] == "Upload release candidate"
    )
    assert compare_bytes_index < compare_index < upload_index

    byte_compare = steps[compare_bytes_index]["run"]
    assert "sha256sum" in byte_compare
    assert "cmp" in byte_compare
    assert "candidate-one" in byte_compare
    assert "candidate-two" in byte_compare

    compare = steps[compare_index]
    assert "python -m scripts.semantic_release compare" in compare["run"]
    assert "uv run --frozen" in compare["run"]
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
    assert check["outputs"]["release_commit"] == "${{ steps.check.outputs.release_commit }}"

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
    assert verify["env"]["RELEASE_COMMIT"] == (
        "${{ needs.check-release-needed.outputs.release_commit }}"
    )
    assert '--expected-commit "${RELEASE_COMMIT}"' in verify["run"]

    notify = _jobs()["notify-downstream"]
    assert notify["if"] == "needs.release.result == 'success'"


def test_release_build_clock_comes_only_from_spec_timestamp():
    jobs = _jobs()
    version = next(
        step
        for step in jobs["release-metadata"]["steps"]
        if step["name"] == "Determine version from metadata"
    )
    build = next(
        step
        for step in jobs["build-release-candidate"]["steps"]
        if step["name"] == "Build release candidate"
    )

    assert 'document.get("spec_timestamp")' in version["run"]
    assert "download_timestamp" not in version["run"]
    assert "build_timestamp=${BUILD_TIMESTAMP}" in version["run"]
    assert "astimezone(UTC)" in version["run"]
    assert build["env"]["BUILD_TIMESTAMP"] == (
        "${{ needs.release-metadata.outputs.build_timestamp }}"
    )
    assert '--build-timestamp "${BUILD_TIMESTAMP}"' in build["run"]


def test_release_notes_render_the_measured_semantic_decision():
    release_steps = _jobs()["release"]["steps"]
    notes = next(step for step in release_steps if step["name"] == "Generate release notes")
    command = notes["run"]

    assert "python -m scripts.semantic_release notes" in command
    assert "uv run --frozen" in command
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


def test_failure_tracker_tracks_pipeline_health_without_inferring_delivery_state():
    tracker = _jobs()["failure-tracker"]
    assert tracker["name"] == "Producer Release Failure Tracker"
    assert set(tracker["needs"]) == {
        "validate",
        "check-release-needed",
        "release",
        "notify-downstream",
        "skip-release",
        "release-metadata",
        "build-release-candidate",
        "commit-release-specs",
    }
    assert "update-docs" not in tracker["needs"]
    assert tracker["if"] == "${{ always() }}"

    tracker_steps = tracker["steps"]
    reconcile = next(
        step
        for step in tracker_steps
        if step["name"] == "Reconcile the failure tracker with this run"
    )
    assert "'failure'" in reconcile["env"]["PIPELINE_UNHEALTHY"]
    assert "'cancelled'" in reconcile["env"]["PIPELINE_UNHEALTHY"]
    assert reconcile["env"]["PUBLISH_SPECS_RESULT"] == ("${{ needs.commit-release-specs.result }}")
    script = reconcile["with"]["script"]

    assert "validate-and-release-producer-failure-tracker" in script
    assert "Publish Regenerated Specs" in script
    assert "publicationRecoverySkipped" not in script
    assert "keeping the tracker open" not in script
    assert "No release artifact publishes while this is red" not in script
    assert "Publication and delivery state are remeasured" in script


def test_dispatch_success_precedes_durable_exact_receipt_acknowledgement():
    notify = _jobs()["notify-downstream"]
    assert notify["permissions"]["deployments"] == "write"
    steps = notify["steps"]
    dispatch_index = next(
        index
        for index, step in enumerate(steps)
        if step["name"] == "Dispatch upstream-specs-released to api-specs-enriched"
    )
    ack_index = next(
        index
        for index, step in enumerate(steps)
        if step["name"] == "Record exact downstream delivery acknowledgement"
    )
    assert dispatch_index < ack_index
    ack = steps[ack_index]
    assert ack["env"]["ACK_TOKEN"] == "${{ github.token }}"
    assert ack["env"]["RELEASE_RECEIPT"] == "${{ needs.release.outputs.release_receipt }}"
    assert ack["env"]["RELEASE_COMMIT"] == (
        "${{ needs.check-release-needed.outputs.release_commit }}"
    )
    assert "python -m scripts.dispatch_ack record" in ack["run"]


def test_release_builder_toolchain_is_exact_locked_and_reproduced_in_fresh_environments():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert workflow["env"]["PYTHON_VERSION"] == "3.11.13"
    assert workflow["env"]["NODE_VERSION"] == "24.19.0"
    assert LOCKFILE.is_file()
    assert "uv.lock" in workflow[True]["push"]["paths"]

    jobs = _jobs()
    check = jobs["check-release-needed"]
    assert check["permissions"]["deployments"] == "read"
    for job_name in ("release-metadata", "build-release-candidate", "check-release-needed"):
        job = jobs[job_name]
        assert job["runs-on"] == "ubuntu-latest"
        install = next(step for step in job["steps"] if step["name"] == "Sync locked environment")
        assert "uv sync --frozen" in install["run"]
        assert "pip install" not in install["run"]

    builder = jobs["build-release-candidate"]
    assert builder["strategy"]["matrix"]["candidate"] == ["one", "two"]
    build = next(step for step in builder["steps"] if step["name"] == "Build release candidate")
    assert build["run"].count("uv run --isolated --frozen") == 1
    assert "cmp" not in build["run"]
    comparison = next(
        step for step in check["steps"] if step["name"] == "Compare independent release candidates"
    )
    assert "cmp" in comparison["run"]
    assert "sha256sum" in comparison["run"]
    assert "python -m scripts.release" not in comparison["run"]

    validate = _jobs()["validate"]
    spectral_install = next(
        step for step in validate["steps"] if step["name"] == "Install Spectral"
    )
    assert "npm ci --ignore-scripts" in spectral_install["run"]
    assert "npm audit --audit-level=high" in spectral_install["run"]
    assert spectral_install["run"].index("npm ci") < spectral_install["run"].index("npm audit")
    validate_source = "\n".join(step.get("run", "") for step in validate["steps"])
    assert "uv sync --frozen" in validate_source
    assert "pip install" not in validate_source
    assert validate_source.count("uv run --frozen python -m scripts.") >= 7


def test_every_direct_action_in_release_workflow_is_commit_pinned():
    action_refs = re.findall(r"^\s*uses:\s+([^\s@]+)@([^\s#]+)", WORKFLOW.read_text(), re.MULTILINE)
    assert action_refs
    for action, ref in action_refs:
        assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{action}@{ref} is mutable"


def test_python_test_workflow_uses_the_same_locked_toolchain():
    workflow = yaml.safe_load(TEST_WORKFLOW.read_text())
    assert workflow["env"]["PYTHON_VERSION"] == "3.11.13"
    assert workflow["env"]["UV_VERSION"] == "0.12.1"
    source = TEST_WORKFLOW.read_text()
    assert "uv sync --frozen --extra dev" in source
    assert "pip install" not in source
    for action, ref in re.findall(r"^\s*uses:\s+([^\s@]+)@([^\s#]+)", source, re.MULTILINE):
        assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{action}@{ref} is mutable"


def test_release_workflow_parameters_contract():
    """Verify parameters contract such as --allow-discrepancies and --reconciliation-report-out."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    jobs = workflow["jobs"]
    validate = jobs["validate"]

    # 1. Verify --allow-discrepancies is present in validation step
    validation_step = next(
        step for step in validate["steps"] if step.get("name") == "Validate specs (live)"
    )
    assert "--allow-discrepancies" in validation_step["run"]

    # 2. Verify --reconciliation-report-out is present in reconcile step
    reconcile_step = next(
        step for step in validate["steps"] if step.get("name") == "Reconcile specs and apply fixes"
    )
    assert "--reconciliation-report-out reports/reconciliation_report.json" in reconcile_step["run"]

    # 3. Verify that the generated documentation path contract is correctly configured
    publish_docs = jobs["update-docs"]
    generate_docs_step = next(
        step for step in publish_docs["steps"] if step.get("name") == "Generate documentation"
    )
    assert "--reconciliation-report reports/reconciliation_report.json" in generate_docs_step["run"]
    assert "--output docs/en/01-validation-report.mdx" in generate_docs_step["run"]


def test_release_workflow_security_and_push_contract():
    """Verify security protections: persist-credentials is false on checkouts,

    and git push utilizes ephemeral GIT_ASKPASS with credential-free remote URLs.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text())
    jobs = workflow["jobs"]

    checkout_steps = []
    push_steps = []

    for job_name, job_def in jobs.items():
        if "steps" in job_def:
            for step in job_def["steps"]:
                if step.get("uses", "").startswith("actions/checkout"):
                    checkout_steps.append((job_name, step))
                if "run" in step and "git push" in step["run"]:
                    push_steps.append((job_name, step))

    # 1. Assert exactly 8 checkout steps
    assert len(checkout_steps) == 8, f"Expected 8 checkout steps, found {len(checkout_steps)}"

    # 2. Assert every checkout step has persist-credentials: False
    for job_name, step in checkout_steps:
        with_params = step.get("with", {})
        assert "persist-credentials" in with_params, (
            f"persist-credentials missing in job {job_name}"
        )
        # Ensure it is a boolean False, not string "false"
        assert with_params["persist-credentials"] is False, (
            f"persist-credentials must be boolean False in job {job_name}"
        )

    # 3. Assert exactly 2 authenticated push steps (under update-docs and commit-release-specs)
    assert len(push_steps) == 2, f"Expected exactly 2 push steps, found {len(push_steps)}"
    expected_jobs = {"update-docs", "commit-release-specs"}
    found_jobs = {job_name for job_name, _ in push_steps}
    assert found_jobs == expected_jobs, (
        f"Expected push steps in jobs {expected_jobs}, found in {found_jobs}"
    )

    # 4. Assert no git push contains token or GH_TOKEN interpolation in URL, and uses GIT_ASKPASS
    for job_name, step in push_steps:
        run_script = step["run"]
        # Ensure no token or interpolation in any git push line
        git_push_lines = [line for line in run_script.splitlines() if "git push" in line]
        for line in git_push_lines:
            assert "x-access-token" not in line, (
                f"Found explicit token in git push of job {job_name}"
            )
            assert "${GH_TOKEN}" not in line, (
                f"Found token variable interpolation in git push URL of job {job_name}"
            )
            # Ensure GIT_ASKPASS and GIT_TERMINAL_PROMPT=0 are used
            assert "GIT_ASKPASS=" in line, f"GIT_ASKPASS must be set on push line in job {job_name}"
            assert "GIT_TERMINAL_PROMPT=0" in line, (
                f"GIT_TERMINAL_PROMPT=0 must be set on push line in job {job_name}"
            )
            # Ensure the push target is credential-free (uses github.com URL directly)
            assert "github.com/" in line, (
                f"Should push to a direct credential-free remote URL, found: {line}"
            )

        # 5. Ensure the askpass helper is created securely and ephemeral cleanup is used
        assert 'ASKPASS_PATH="' in run_script, f"ASKPASS_PATH not defined in job {job_name}"
        assert "echo 'echo \"${GH_TOKEN}\"'" in run_script, (
            f"Askpass must print GH_TOKEN directly from environment in job {job_name}"
        )
        assert "chmod 700" in run_script, (
            f"Askpass helper must have restricted 700 permissions in job {job_name}"
        )
        assert "trap " in run_script and "rm -f" in run_script, (
            f"Askpass helper must use trap for cleanup in job {job_name}"
        )


def test_release_workflow_explicit_environments():
    """Verify that jobs in validate-and-release.yml are explicitly mapped to their correct security environments."""
    jobs = _jobs()
    assert jobs["validate"]["environment"] == "f5xc-live-validation"
    assert jobs["release"]["environment"] == "repository-settings"
    assert jobs["notify-downstream"]["environment"] == "api-specs-enriched-release-delivery"
    assert jobs["update-docs"]["environment"] == "api-specs-publication"
    assert jobs["commit-release-specs"]["environment"] == "api-specs-publication"


def test_narrower_token_selection_and_no_admin_token():
    """Verify that secrets.REPO_ADMIN_TOKEN is eliminated and REPO_SYNC_TOKEN is used with a fail-fast checker."""
    workflow_text = WORKFLOW.read_text()
    assert "secrets.REPO_ADMIN_TOKEN" not in workflow_text, (
        "secrets.REPO_ADMIN_TOKEN must be completely eliminated"
    )

    jobs = _jobs()
    for job_name in ("update-docs", "commit-release-specs"):
        job = jobs[job_name]
        steps = job["steps"]

        # Check checkout step token
        checkout_step = next(
            step for step in steps if step.get("uses", "").startswith("actions/checkout")
        )
        assert checkout_step["with"]["token"] == "${{ secrets.REPO_SYNC_TOKEN }}"

        # Check fail-fast helper checking REPO_SYNC_TOKEN liveness
        fail_fast_step = next(step for step in steps if "Fail fast if" in step.get("name", ""))
        assert "REPO_SYNC_TOKEN" in fail_fast_step["env"]["SYNC_TOKEN"]
        assert 'if [ -z "${SYNC_TOKEN}" ]' in fail_fast_step["run"]

        # Check commit/push step token
        commit_step = next(
            step
            for step in steps
            if "Commit and PR" in step.get("name", "") or "Open a PR if" in step.get("name", "")
        )
        assert commit_step["env"]["GH_TOKEN"] == "${{ secrets.REPO_SYNC_TOKEN }}"


def test_deterministic_uv_in_documentation_setup():
    """Verify that update-docs uses deterministic uv commands and no raw pip."""
    jobs = _jobs()
    steps = jobs["update-docs"]["steps"]

    # Assert setup-uv and uv sync --frozen are used
    assert any(step.get("uses", "").startswith("astral-sh/setup-uv") for step in steps)
    assert any("uv sync --frozen" in step.get("run", "") for step in steps)
    assert not any("pip install" in step.get("run", "") for step in steps)

    # Assert generate step uses uv run --frozen
    generate_step = next(step for step in steps if step.get("name") == "Generate documentation")
    assert "uv run --frozen python" in generate_step["run"]


def test_tests_workflow_checkout_persist_credentials():
    """Verify that all actions/checkout steps in tests.yml have persist-credentials explicitly set to False."""
    test_wf = yaml.safe_load(TEST_WORKFLOW.read_text())
    found_checkout = False
    for job_name, job_def in test_wf["jobs"].items():
        if "steps" in job_def:
            for step in job_def["steps"]:
                if step.get("uses", "").startswith("actions/checkout"):
                    found_checkout = True
                    assert "persist-credentials" in step.get("with", {}), (
                        f"persist-credentials parameter is missing in tests.yml job {job_name}"
                    )
                    assert step["with"]["persist-credentials"] is False, (
                        f"persist-credentials must be boolean False in tests.yml job {job_name}"
                    )
    assert found_checkout, "Expected to find actions/checkout in tests.yml"


def test_node24_action_hashes_are_strictly_enforced():
    """Verify that all checkouts, setup-python, setup-node, cache, and upload-artifact steps in release and test workflows use specific Node 24 hashes."""
    expected_hashes = {
        "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
        "actions/setup-node": "820762786026740c76f36085b0efc47a31fe5020",
        "actions/cache": "55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
        "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    }

    for workflow_path in (WORKFLOW, TEST_WORKFLOW):
        content = workflow_path.read_text()
        action_refs = re.findall(r"^\s*uses:\s+([^\s@]+)@([^\s#\n\s]+)", content, re.MULTILINE)
        for action, ref in action_refs:
            if action in expected_hashes:
                assert ref == expected_hashes[action], (
                    f"In {workflow_path.name}, action {action} is pinned to {ref}, "
                    f"expected Node 24 hash {expected_hashes[action]}"
                )


def test_no_direct_github_repository_interpolation_in_run_blocks():
    """Verify that ${{ github.repository }} is not directly interpolated into any run: scripts of validate-and-release.yml to prevent template-injection."""
    jobs = _jobs()
    for job_name, job_def in jobs.items():
        if "steps" in job_def:
            for step in job_def["steps"]:
                if "run" in step:
                    run_script = step["run"]
                    assert "${{ github.repository }}" not in run_script, (
                        f"Direct interpolation of ${{ github.repository }} is forbidden in run script of step '{step.get('name', 'unnamed')}' in job '{job_name}' to prevent template-injection. Use step-level env instead."
                    )
