"""Constraint validation logic for OpenAPI specifications."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rich.console import Console

console = Console()


class DiscrepancyType(Enum):
    """Types of discrepancies between spec and live API."""

    SPEC_STRICTER = "spec_stricter"  # Spec rejects what API accepts
    SPEC_LOOSER = "spec_looser"  # Spec accepts what API rejects
    MISSING_CONSTRAINT = "missing_constraint"  # Spec lacks a constraint API enforces
    EXTRA_CONSTRAINT = "extra_constraint"  # Spec has constraint API ignores
    CONSTRAINT_MISMATCH = "constraint_mismatch"  # Different constraint values
    TYPE_MISMATCH = "type_mismatch"  # Different data types


@dataclass
class ValidationResult:
    """Result of a constraint validation test."""

    valid: bool
    test_value: Any
    constraint_type: str
    spec_constraint: Any
    api_response: dict | None = None
    api_accepted: bool | None = None
    error_message: str | None = None


@dataclass
class Discrepancy:
    """A discrepancy between spec constraint and API behavior."""

    path: str
    property_name: str
    constraint_type: str
    discrepancy_type: DiscrepancyType
    spec_value: Any
    api_behavior: Any
    test_values: list[Any] = field(default_factory=list)
    recommendation: str = ""


@dataclass
class ValidationTestCase:
    """A test case for constraint validation."""

    name: str
    value: Any
    expected_valid: bool  # What spec says
    description: str = ""


class ConstraintValidator:
    """Validate constraints and generate test cases."""

    def __init__(self):
        self._test_generators: dict[str, Callable] = {
            "minLength": self._generate_min_length_tests,
            "maxLength": self._generate_max_length_tests,
            "pattern": self._generate_pattern_tests,
            "minimum": self._generate_minimum_tests,
            "maximum": self._generate_maximum_tests,
            "exclusiveMinimum": self._generate_exclusive_minimum_tests,
            "exclusiveMaximum": self._generate_exclusive_maximum_tests,
            "minItems": self._generate_min_items_tests,
            "maxItems": self._generate_max_items_tests,
            "uniqueItems": self._generate_unique_items_tests,
            "enum": self._generate_enum_tests,
            "type": self._generate_type_tests,
            "required": self._generate_required_tests,
        }

    def generate_test_cases(
        self,
        constraint_type: str,
        constraint_value: Any,
        property_schema: dict | None = None,
    ) -> list[ValidationTestCase]:
        """Generate test cases for a specific constraint."""
        generator = self._test_generators.get(constraint_type)
        if generator:
            return generator(constraint_value, property_schema or {})
        return []

    def _generate_min_length_tests(
        self,
        min_length: int,
        schema: dict,
    ) -> list[ValidationTestCase]:
        """Generate tests for minLength constraint."""
        tests = []

        # Value at exactly min length (should pass)
        tests.append(
            ValidationTestCase(
                name=f"minLength_exact_{min_length}",
                value="a" * min_length,
                expected_valid=True,
                description=f"String of exactly {min_length} characters",
            )
        )

        # Value below min length (should fail)
        if min_length > 0:
            tests.append(
                ValidationTestCase(
                    name=f"minLength_below_{min_length}",
                    value="a" * (min_length - 1),
                    expected_valid=False,
                    description=f"String of {min_length - 1} characters (below minimum)",
                )
            )

        # Empty string (should fail if minLength > 0)
        if min_length > 0:
            tests.append(
                ValidationTestCase(
                    name="minLength_empty",
                    value="",
                    expected_valid=False,
                    description="Empty string",
                )
            )

        # Value above min length (should pass)
        tests.append(
            ValidationTestCase(
                name=f"minLength_above_{min_length}",
                value="a" * (min_length + 5),
                expected_valid=True,
                description=f"String of {min_length + 5} characters (above minimum)",
            )
        )

        return tests

    def _generate_max_length_tests(
        self,
        max_length: int,
        schema: dict,
    ) -> list[ValidationTestCase]:
        """Generate tests for maxLength constraint."""
        tests = []

        # Value at exactly max length (should pass)
        tests.append(
            ValidationTestCase(
                name=f"maxLength_exact_{max_length}",
                value="a" * max_length,
                expected_valid=True,
                description=f"String of exactly {max_length} characters",
            )
        )

        # Value above max length (should fail)
        tests.append(
            ValidationTestCase(
                name=f"maxLength_above_{max_length}",
                value="a" * (max_length + 1),
                expected_valid=False,
                description=f"String of {max_length + 1} characters (above maximum)",
            )
        )

        # Value well above max length (should fail)
        tests.append(
            ValidationTestCase(
                name=f"maxLength_overflow_{max_length}",
                value="a" * (max_length + 100),
                expected_valid=False,
                description=f"String of {max_length + 100} characters (overflow)",
            )
        )

        # Empty string (should pass)
        tests.append(
            ValidationTestCase(
                name="maxLength_empty",
                value="",
                expected_valid=True,
                description="Empty string",
            )
        )

        return tests

    def _generate_pattern_tests(
        self,
        pattern: str,
        schema: dict,
    ) -> list[ValidationTestCase]:
        """Generate tests for pattern constraint."""
        tests = []

        try:
            regex = re.compile(pattern)

            # Generate valid patterns
            valid_samples = self._generate_matching_strings(pattern)
            for i, sample in enumerate(valid_samples[:3]):
                tests.append(
                    ValidationTestCase(
                        name=f"pattern_valid_{i}",
                        value=sample,
                        expected_valid=True,
                        description=f"String matching pattern: {sample}",
                    )
                )

            # Generate invalid patterns
            invalid_samples = self._generate_non_matching_strings(pattern)
            for i, sample in enumerate(invalid_samples[:3]):
                if not regex.match(sample):  # Verify it doesn't match
                    tests.append(
                        ValidationTestCase(
                            name=f"pattern_invalid_{i}",
                            value=sample,
                            expected_valid=False,
                            description=f"String not matching pattern: {sample}",
                        )
                    )

        except re.error:
            console.print(f"[yellow]Invalid regex pattern: {pattern}[/yellow]")

        return tests

    def _generate_matching_strings(self, pattern: str) -> list[str]:
        """Generate strings that should match a pattern (heuristic)."""
        samples = []

        # Common F5 XC naming patterns
        if pattern in (r"^[a-z][a-z0-9-]*$", r"^[a-z0-9][a-z0-9-]*$"):
            samples = ["test-name", "my-resource-1", "a", "abc123"]
        elif "^[a-zA-Z]" in pattern:
            samples = ["TestName", "myResource", "ABC"]
        elif "^[0-9]" in pattern:
            samples = ["123", "1test", "999"]
        else:
            # Generic alphanumeric
            samples = ["test", "test123", "test-123"]

        return samples

    def _generate_non_matching_strings(self, pattern: str) -> list[str]:
        """Generate strings that should NOT match a pattern."""
        invalids = [
            "123test",  # Starts with number (often invalid)
            "-test",  # Starts with hyphen
            "TEST_NAME",  # Uppercase and underscore
            "test name",  # Space
            "",  # Empty
            "test!@#",  # Special characters
        ]
        return invalids

    def _generate_minimum_tests(
        self,
        minimum: float,
        schema: dict,
    ) -> list[ValidationTestCase]:
        """Generate tests for minimum constraint."""
        tests = []

        # At minimum (should pass)
        tests.append(
            ValidationTestCase(
                name=f"minimum_exact_{minimum}",
                value=minimum,
                expected_valid=True,
                description=f"Value exactly at minimum ({minimum})",
            )
        )

        # Below minimum (should fail)
        tests.append(
            ValidationTestCase(
                name=f"minimum_below_{minimum}",
                value=minimum - 1,
                expected_valid=False,
                description=f"Value below minimum ({minimum - 1})",
            )
        )

        # Above minimum (should pass)
        tests.append(
            ValidationTestCase(
                name=f"minimum_above_{minimum}",
                value=minimum + 1,
                expected_valid=True,
                description=f"Value above minimum ({minimum + 1})",
            )
        )

        return tests

    def _generate_maximum_tests(
        self,
        maximum: float,
        schema: dict,
    ) -> list[ValidationTestCase]:
        """Generate tests for maximum constraint."""
        tests = []

        # At maximum (should pass)
        tests.append(
            ValidationTestCase(
                name=f"maximum_exact_{maximum}",
                value=maximum,
                expected_valid=True,
                description=f"Value exactly at maximum ({maximum})",
            )
        )

        # Above maximum (should fail)
        tests.append(
            ValidationTestCase(
                name=f"maximum_above_{maximum}",
                value=maximum + 1,
                expected_valid=False,
                description=f"Value above maximum ({maximum + 1})",
            )
        )

        # Below maximum (should pass)
        tests.append(
            ValidationTestCase(
                name=f"maximum_below_{maximum}",
                value=maximum - 1,
                expected_valid=True,
                description=f"Value below maximum ({maximum - 1})",
            )
        )

        return tests

    def _generate_exclusive_minimum_tests(
        self,
        exclusive_min: float,
        schema: dict,
    ) -> list[ValidationTestCase]:
        """Generate tests for exclusiveMinimum constraint."""
        tests = []

        # At boundary (should fail - exclusive)
        tests.append(
            ValidationTestCase(
                name=f"exclusiveMinimum_exact_{exclusive_min}",
                value=exclusive_min,
                expected_valid=False,
                description=f"Value at exclusive minimum ({exclusive_min})",
            )
        )

        # Just above (should pass)
        tests.append(
            ValidationTestCase(
                name=f"exclusiveMinimum_above_{exclusive_min}",
                value=exclusive_min + 0.001,
                expected_valid=True,
                description=f"Value just above exclusive minimum ({exclusive_min + 0.001})",
            )
        )

        return tests

    def _generate_exclusive_maximum_tests(
        self,
        exclusive_max: float,
        schema: dict,
    ) -> list[ValidationTestCase]:
        """Generate tests for exclusiveMaximum constraint."""
        tests = []

        # At boundary (should fail - exclusive)
        tests.append(
            ValidationTestCase(
                name=f"exclusiveMaximum_exact_{exclusive_max}",
                value=exclusive_max,
                expected_valid=False,
                description=f"Value at exclusive maximum ({exclusive_max})",
            )
        )

        # Just below (should pass)
        tests.append(
            ValidationTestCase(
                name=f"exclusiveMaximum_below_{exclusive_max}",
                value=exclusive_max - 0.001,
                expected_valid=True,
                description=f"Value just below exclusive maximum ({exclusive_max - 0.001})",
            )
        )

        return tests

    def _generate_min_items_tests(
        self,
        min_items: int,
        schema: dict,
    ) -> list[ValidationTestCase]:
        """Generate tests for minItems constraint."""
        tests = []

        # Exactly min items (should pass)
        tests.append(
            ValidationTestCase(
                name=f"minItems_exact_{min_items}",
                value=["item"] * min_items,
                expected_valid=True,
                description=f"Array with exactly {min_items} items",
            )
        )

        # Below min items (should fail)
        if min_items > 0:
            tests.append(
                ValidationTestCase(
                    name=f"minItems_below_{min_items}",
                    value=["item"] * (min_items - 1),
                    expected_valid=False,
                    description=f"Array with {min_items - 1} items",
                )
            )

        # Empty array (should fail if minItems > 0)
        if min_items > 0:
            tests.append(
                ValidationTestCase(
                    name="minItems_empty",
                    value=[],
                    expected_valid=False,
                    description="Empty array",
                )
            )

        return tests

    def _generate_max_items_tests(
        self,
        max_items: int,
        schema: dict,
    ) -> list[ValidationTestCase]:
        """Generate tests for maxItems constraint."""
        tests = []

        # Exactly max items (should pass)
        tests.append(
            ValidationTestCase(
                name=f"maxItems_exact_{max_items}",
                value=["item"] * max_items,
                expected_valid=True,
                description=f"Array with exactly {max_items} items",
            )
        )

        # Above max items (should fail)
        tests.append(
            ValidationTestCase(
                name=f"maxItems_above_{max_items}",
                value=["item"] * (max_items + 1),
                expected_valid=False,
                description=f"Array with {max_items + 1} items",
            )
        )

        # Empty array (should pass)
        tests.append(
            ValidationTestCase(
                name="maxItems_empty",
                value=[],
                expected_valid=True,
                description="Empty array",
            )
        )

        return tests

    def _generate_unique_items_tests(
        self,
        unique_items: bool,
        schema: dict,
    ) -> list[ValidationTestCase]:
        """Generate tests for uniqueItems constraint."""
        tests = []

        if unique_items:
            # Unique items (should pass)
            tests.append(
                ValidationTestCase(
                    name="uniqueItems_unique",
                    value=["a", "b", "c"],
                    expected_valid=True,
                    description="Array with unique items",
                )
            )

            # Duplicate items (should fail)
            tests.append(
                ValidationTestCase(
                    name="uniqueItems_duplicate",
                    value=["a", "b", "a"],
                    expected_valid=False,
                    description="Array with duplicate items",
                )
            )

        return tests

    def _generate_enum_tests(
        self,
        enum_values: list,
        schema: dict,
    ) -> list[ValidationTestCase]:
        """Generate tests for enum constraint."""
        tests = []

        # Valid enum values
        for i, value in enumerate(enum_values[:3]):  # Test up to 3 valid values
            tests.append(
                ValidationTestCase(
                    name=f"enum_valid_{i}",
                    value=value,
                    expected_valid=True,
                    description=f"Valid enum value: {value}",
                )
            )

        # Invalid enum value
        invalid_value = "INVALID_ENUM_VALUE_12345"
        if invalid_value not in enum_values:
            tests.append(
                ValidationTestCase(
                    name="enum_invalid",
                    value=invalid_value,
                    expected_valid=False,
                    description=f"Invalid enum value: {invalid_value}",
                )
            )

        return tests

    def _generate_type_tests(
        self,
        expected_type: str,
        schema: dict,
    ) -> list[ValidationTestCase]:
        """Generate tests for type constraint."""
        tests = []

        # Type test values
        type_examples = {
            "string": ("test_string", 123, True),
            "integer": (42, "not_int", 3.14),
            "number": (3.14, "not_number", True),
            "boolean": (True, "not_bool", 1),
            "array": (["item"], "not_array", {}),
            "object": ({"key": "value"}, "not_object", []),
        }

        if expected_type in type_examples:
            valid, *invalids = type_examples[expected_type]

            # Valid type
            tests.append(
                ValidationTestCase(
                    name=f"type_valid_{expected_type}",
                    value=valid,
                    expected_valid=True,
                    description=f"Valid {expected_type} value",
                )
            )

            # Invalid types
            for i, invalid in enumerate(invalids):
                tests.append(
                    ValidationTestCase(
                        name=f"type_invalid_{expected_type}_{i}",
                        value=invalid,
                        expected_valid=False,
                        description=f"Invalid type (expected {expected_type})",
                    )
                )

        return tests

    def _generate_required_tests(
        self,
        required_fields: list[str],
        schema: dict,
    ) -> list[ValidationTestCase]:
        """Generate tests for required fields."""
        tests = []

        # Test omitting each required field
        for field_name in required_fields:
            tests.append(
                ValidationTestCase(
                    name=f"required_missing_{field_name}",
                    value={"_omit_field": field_name},
                    expected_valid=False,
                    description=f"Missing required field: {field_name}",
                )
            )

        return tests

    def compare_results(
        self,
        test_case: ValidationTestCase,
        api_accepted: bool,
    ) -> Discrepancy | None:
        """Compare test case expectation with API behavior."""
        if test_case.expected_valid == api_accepted:
            return None  # No discrepancy

        if test_case.expected_valid and not api_accepted:
            # Spec says valid, API rejects → API is stricter
            discrepancy_type = DiscrepancyType.SPEC_LOOSER
        else:
            # Spec says invalid, API accepts → Spec is stricter
            discrepancy_type = DiscrepancyType.SPEC_STRICTER

        return Discrepancy(
            path="",  # Will be set by caller
            property_name="",  # Will be set by caller
            constraint_type=test_case.name.split("_")[0],
            discrepancy_type=discrepancy_type,
            spec_value=test_case.expected_valid,
            api_behavior=api_accepted,
            test_values=[test_case.value],
        )


def create_validator() -> ConstraintValidator:
    """Create a new constraint validator instance."""
    return ConstraintValidator()
