"""Unit tests for validation orchestrator CLI parameters."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from scripts.utils.constraint_validator import DiscrepancyType
from scripts.validate import Discrepancy, ValidationOrchestrator, ValidationTarget


def test_allow_discrepancies_suppresses_exit_code() -> None:
    # Instantiate ValidationOrchestrator with mocked components
    config: dict[str, Any] = {}
    endpoints_config: dict[str, Any] = {}
    auth = MagicMock()

    orchestrator = ValidationOrchestrator(config, endpoints_config, auth)

    # Use patch.object to mock methods safely without violating typing descriptor assignments
    with (
        patch.object(orchestrator, "_load_specs", return_value={"test.json": {}}),
        patch.object(orchestrator, "_validate_spec_structure", return_value={}),
        patch.object(orchestrator, "_generate_reports"),
        patch.object(orchestrator, "_print_summary"),
        patch(
            "scripts.validate.resolve_validation_targets",
            return_value=(ValidationTarget("healthcheck", "healthcheck", "test.json", ()),),
        ),
    ):
        # 1. No discrepancies, normal run -> returns 0
        with patch.object(orchestrator, "_run_schemathesis_tests", return_value=[]):
            orchestrator.discrepancies = []
            assert orchestrator.run(allow_discrepancies=False) == 0

        # 2. Discrepancies exist, allow_discrepancies=False -> returns 1
        with patch.object(orchestrator, "_run_schemathesis_tests", return_value=[]):
            orchestrator.discrepancies = [
                Discrepancy(
                    path="TestModel",
                    property_name="TestModel",
                    constraint_type="maxLength",
                    discrepancy_type=DiscrepancyType.SPEC_STRICTER,
                    spec_file="test.json",
                    spec_value=10,
                    api_behavior=20,
                )
            ]
            assert orchestrator.run(allow_discrepancies=False) == 1

            # 3. Discrepancies exist, allow_discrepancies=True -> returns 0
            assert orchestrator.run(allow_discrepancies=True) == 0

        # 4. Live execution errors exist -> returns 1 regardless of allow_discrepancies
        with patch.object(
            orchestrator, "_run_schemathesis_tests", return_value=["Connection refused"]
        ):
            assert orchestrator.run(allow_discrepancies=True) == 1
