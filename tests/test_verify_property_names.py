"""Tests for recording live-API observations on property name corrections."""

from __future__ import annotations

from scripts.verify_property_names import _observed_status, _update_config

SCHEMA = "WidgetSpec"
TYPO_KEY = "widget_naem"
FIXED_KEY = "widget_name"


def _correction(**overrides) -> dict:
    correction = {
        "schema": SCHEMA,
        "old_key": TYPO_KEY,
        "new_key": FIXED_KEY,
        "verified": False,
        "api_status": "unverifiable",
    }
    correction.update(overrides)
    return correction


def _result(status: str) -> dict:
    return {
        "schema": SCHEMA,
        "old_key": TYPO_KEY,
        "new_key": FIXED_KEY,
        "status": status,
    }


class TestUpdateConfig:
    def test_fix_spec_marks_verified_and_records_the_status(self):
        corrections = [_correction()]
        assert _update_config(corrections, [_result("fix_spec")]) is True
        assert corrections[0]["verified"] is True
        assert corrections[0]["api_status"] == "fix_spec"

    def test_upstream_typo_also_earns_verification(self):
        """Confirming the typo is a real observation, not a failed probe."""
        corrections = [_correction()]
        assert _update_config(corrections, [_result("upstream_typo")]) is True
        assert corrections[0]["verified"] is True
        assert corrections[0]["api_status"] == "upstream_typo"

    def test_permanent_typo_is_not_downgraded(self):
        corrections = [_correction(verified=True, api_status="upstream_typo_permanent")]
        assert _update_config(corrections, [_result("upstream_typo")]) is False
        assert corrections[0]["api_status"] == "upstream_typo_permanent"

    def test_permanent_typo_is_never_promoted_to_fix_spec(self):
        """A read probe must not unlock a rename the platform ignores on write.

        ``blocked_service`` is accepted by the request parser and then dropped,
        so a GET that happens to surface the corrected key is not evidence the
        wire key may move -- see terraform-provider-xcsh#1257.
        """
        corrections = [_correction(verified=True, api_status="upstream_typo_permanent")]
        assert _update_config(corrections, [_result("fix_spec")]) is False
        assert corrections[0]["api_status"] == "upstream_typo_permanent"

    def test_unusable_probe_leaves_the_entry_alone(self):
        for status in (
            "http_403",
            "http_503",
            "neither_found",
            "both_present",
            "error",
        ):
            corrections = [_correction()]
            assert _update_config(corrections, [_result(status)]) is False
            assert corrections[0]["verified"] is False
            assert corrections[0]["api_status"] == "unverifiable"

    def test_already_recorded_observation_is_not_rewritten(self):
        corrections = [_correction(verified=True, api_status="upstream_typo")]
        assert _update_config(corrections, [_result("upstream_typo")]) is False

    def test_a_changed_observation_is_recorded(self):
        """F5 fixing the platform must flip the entry back to fix_spec."""
        corrections = [_correction(verified=True, api_status="upstream_typo")]
        assert _update_config(corrections, [_result("fix_spec")]) is True
        assert corrections[0]["api_status"] == "fix_spec"

    def test_only_the_matching_entry_is_touched(self):
        other = _correction(old_key="gadget_kolor", new_key="gadget_color")
        corrections = [_correction(), other]
        _update_config(corrections, [_result("upstream_typo")])
        assert other["verified"] is False


class TestObservedStatus:
    def test_permanent_typo_survives_a_confirming_probe(self):
        correction = _correction(api_status="upstream_typo_permanent")
        assert _observed_status(correction, "upstream_typo") == "upstream_typo_permanent"

    def test_permanent_typo_survives_a_fix_spec_probe(self):
        correction = _correction(api_status="upstream_typo_permanent")
        assert _observed_status(correction, "fix_spec") == "upstream_typo_permanent"
