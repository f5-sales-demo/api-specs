"""Unit tests for validation orchestrator CLI parameters."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from scripts.validate import ValidationOrchestrator, Discrepancy, ValidationTarget


def test_allow_discrepancies_suppresses_exit_code():
    # Instantiate ValidationOrchestrator with mocked components
    config = {}
    endpoints_config = {}
    auth = MagicMock()
    
    orchestrator = ValidationOrchestrator(config, endpoints_config, auth)
    
    # Mock out loading/execution steps so we can isolate the return code behavior in run()
    orchestrator._load_specs = MagicMock(return_value={"test.json": {}})
    orchestrator._validate_spec_structure = MagicMock(return_value={})
    orchestrator._generate_reports = MagicMock()
    orchestrator._print_summary = MagicMock()
    
    # Simulate a resolved target list
    with patch("scripts.validate.resolve_validation_targets", return_value=(ValidationTarget("healthcheck", "healthcheck", "test.json", ()),)):
        # 1. No discrepancies, normal run -> returns 0
        orchestrator._run_schemathesis_tests = MagicMock(return_value=[])
        orchestrator.discrepancies = []
        assert orchestrator.run(allow_discrepancies=False) == 0
        
        # 2. Discrepancies exist, allow_discrepancies=False -> returns 1
        orchestrator.discrepancies = [
            Discrepancy(
                path="TestModel",
                property_name="TestModel",
                constraint_type="maxLength",
                discrepancy_type="spec_stricter",
                spec_file="test.json",
                spec_value=10,
                api_behavior=20,
            )
        ]
        assert orchestrator.run(allow_discrepancies=False) == 1
        
        # 3. Discrepancies exist, allow_discrepancies=True -> returns 0
        assert orchestrator.run(allow_discrepancies=True) == 0
        
        # 4. Live execution errors exist -> returns 1 regardless of allow_discrepancies
        orchestrator._run_schemathesis_tests = MagicMock(return_value=["Connection refused"])
        assert orchestrator.run(allow_discrepancies=True) == 1
