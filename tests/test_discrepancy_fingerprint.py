from scripts.utils.constraint_validator import Discrepancy, DiscrepancyType
from scripts.utils.discrepancy_fingerprint import fingerprint, short_form


def _make(
    path="/a", prop="foo", ctype="minLength", dtype=DiscrepancyType.SPEC_STRICTER
):
    return Discrepancy(
        path=path,
        property_name=prop,
        constraint_type=ctype,
        discrepancy_type=dtype,
        spec_value=1,
        api_behavior={"accepted_min": 0},
    )


def test_fingerprint_is_40_hex_chars():
    fp = fingerprint(_make(), domain="origin_pool", method="POST")
    assert len(fp) == 40
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_is_stable_across_calls():
    d = _make()
    assert fingerprint(d, "origin_pool", "POST") == fingerprint(
        d, "origin_pool", "POST"
    )


def test_fingerprint_changes_with_any_input():
    base = _make()
    baseline = fingerprint(base, "origin_pool", "POST")
    assert fingerprint(base, "origin_pool", "GET") != baseline
    assert fingerprint(base, "app_firewall", "POST") != baseline
    assert fingerprint(_make(path="/b"), "origin_pool", "POST") != baseline
    assert fingerprint(_make(prop="bar"), "origin_pool", "POST") != baseline
    assert (
        fingerprint(_make(dtype=DiscrepancyType.SPEC_LOOSER), "origin_pool", "POST")
        != baseline
    )


def test_short_form_is_first_8_chars():
    fp = fingerprint(_make(), "origin_pool", "POST")
    assert short_form(fp) == fp[:8]


def test_fingerprint_ignores_non_payload_fields():
    """Fields on Discrepancy that are NOT part of the fingerprint payload
    must not change the output. Locks the design intent so future edits
    cannot silently break cross-run stability by folding extra fields in.
    """
    baseline = fingerprint(_make(), "origin_pool", "POST")
    # constraint_type is stored on Discrepancy but excluded from fingerprint.
    assert fingerprint(_make(ctype="maxLength"), "origin_pool", "POST") == baseline
    # spec_value and api_behavior vary freely across runs; must not affect fp.
    drifting = Discrepancy(
        path="/a",
        property_name="foo",
        constraint_type="minLength",
        discrepancy_type=DiscrepancyType.SPEC_STRICTER,
        spec_value=9999,
        api_behavior={"completely": "different"},
    )
    assert fingerprint(drifting, "origin_pool", "POST") == baseline


def test_fingerprint_tolerates_pipe_in_payload_fields():
    """Because the delimiter is \\x1f and never `|`, a pipe in a path or
    property name must not collide with another discrepancy whose fields
    differ only by placement around that pipe.
    """
    a = Discrepancy(
        path="/a|b",
        property_name="",
        constraint_type="minLength",
        discrepancy_type=DiscrepancyType.SPEC_STRICTER,
        spec_value=1,
        api_behavior={},
    )
    b = Discrepancy(
        path="/a",
        property_name="b",
        constraint_type="minLength",
        discrepancy_type=DiscrepancyType.SPEC_STRICTER,
        spec_value=1,
        api_behavior={},
    )
    assert fingerprint(a, "origin_pool", "POST") != fingerprint(
        b, "origin_pool", "POST"
    )
