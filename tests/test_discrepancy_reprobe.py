import httpx

from scripts.utils.constraint_validator import Discrepancy, DiscrepancyType
from scripts.utils.discrepancy_reprobe import ReprobeEvidence, reprobe_discrepancy


def _stricter_disc():
    """Fixture: SPEC says port>=1; live API previously accepted port=0."""
    return Discrepancy(
        path="/public/config/namespaces/system/origin_pools",
        property_name="port",
        constraint_type="minimum",
        discrepancy_type=DiscrepancyType.SPEC_STRICTER,
        spec_value=1,
        api_behavior={"accepted": 0},
        test_values=[0],
    )


def _looser_disc():
    """Fixture: SPEC says name is free-text; live API previously rejected empty."""
    return Discrepancy(
        path="/public/config/namespaces/system/origin_pools",
        property_name="name",
        constraint_type="minLength",
        discrepancy_type=DiscrepancyType.SPEC_LOOSER,
        spec_value=0,
        api_behavior={"rejected": ""},
        test_values=[""],
    )


def _client(handler):
    return httpx.Client(base_url="https://t", transport=httpx.MockTransport(handler))


def test_reprobe_returns_evidence_with_status_and_body():
    def handler(_req):
        return httpx.Response(400, json={"error": "validation failed"})

    evidence = reprobe_discrepancy(
        _stricter_disc(), domain="origin_pool", method="POST", client=_client(handler)
    )
    assert isinstance(evidence, ReprobeEvidence)
    assert evidence.status_code == 400
    assert "validation failed" in evidence.body_snippet
    assert evidence.endpoint_url.endswith("/origin_pools")
    assert evidence.timestamp_utc.endswith("Z")
    assert evidence.method == "POST"
    assert evidence.test_value == 0


def test_reprobe_truncates_large_bodies():
    def handler(_req):
        return httpx.Response(200, text="x" * 10000)

    evidence = reprobe_discrepancy(
        _stricter_disc(), domain="origin_pool", method="POST", client=_client(handler)
    )
    assert len(evidence.body_snippet) <= 2048


def test_spec_stricter_still_present_when_api_still_accepts():
    """SPEC_STRICTER re-probe where API keeps accepting the test value — discrepancy remains."""

    def handler(_req):
        return httpx.Response(201, json={"id": "abc"})

    evidence = reprobe_discrepancy(
        _stricter_disc(), "origin_pool", "POST", client=_client(handler)
    )
    assert evidence.discrepancy_still_present is True


def test_spec_stricter_resolved_when_api_now_rejects():
    """SPEC_STRICTER re-probe where API now rejects — upstream has tightened; discrepancy resolved."""

    def handler(_req):
        return httpx.Response(400, json={"error": "value too small"})

    evidence = reprobe_discrepancy(
        _stricter_disc(), "origin_pool", "POST", client=_client(handler)
    )
    assert evidence.discrepancy_still_present is False


def test_spec_looser_still_present_when_api_still_rejects():
    """SPEC_LOOSER re-probe where API keeps rejecting — discrepancy remains."""

    def handler(_req):
        return httpx.Response(400, json={"error": "name required"})

    evidence = reprobe_discrepancy(
        _looser_disc(), "origin_pool", "POST", client=_client(handler)
    )
    assert evidence.discrepancy_still_present is True


def test_spec_looser_resolved_when_api_now_accepts():
    """SPEC_LOOSER re-probe where API now accepts — upstream has loosened; discrepancy resolved."""

    def handler(_req):
        return httpx.Response(201, json={"id": "abc"})

    evidence = reprobe_discrepancy(
        _looser_disc(), "origin_pool", "POST", client=_client(handler)
    )
    assert evidence.discrepancy_still_present is False


def test_reprobe_unknown_discrepancy_type_defaults_to_still_present():
    """TYPE_MISMATCH and similar types have no automated signal; default to still-present."""
    d = Discrepancy(
        path="/x",
        property_name="n",
        constraint_type="type",
        discrepancy_type=DiscrepancyType.TYPE_MISMATCH,
        spec_value="string",
        api_behavior="integer",
        test_values=[42],
    )

    def handler(_req):
        return httpx.Response(201)

    evidence = reprobe_discrepancy(d, "origin_pool", "POST", client=_client(handler))
    assert evidence.discrepancy_still_present is True
