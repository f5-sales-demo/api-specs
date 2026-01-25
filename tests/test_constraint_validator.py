"""Tests for constraint validator module."""

from __future__ import annotations

import pytest

from scripts.utils.constraint_validator import (
    ConstraintValidator,
    Discrepancy,
    DiscrepancyType,
    ValidationTestCase,
)


class TestConstraintValidator:
    """Tests for ConstraintValidator class."""

    @pytest.fixture
    def validator(self) -> ConstraintValidator:
        """Create a validator instance."""
        return ConstraintValidator()

    # String Length Tests

    def test_min_length_generates_tests(self, validator: ConstraintValidator):
        """Test minLength test case generation."""
        tests = validator.generate_test_cases("minLength", 5, {})

        assert len(tests) >= 3

        # Should have test at exact min
        exact_test = next(t for t in tests if "exact" in t.name)
        assert len(exact_test.value) == 5
        assert exact_test.expected_valid is True

        # Should have test below min
        below_test = next(t for t in tests if "below" in t.name)
        assert len(below_test.value) == 4
        assert below_test.expected_valid is False

    def test_min_length_zero(self, validator: ConstraintValidator):
        """Test minLength=0 (no minimum)."""
        tests = validator.generate_test_cases("minLength", 0, {})

        # Empty string should be valid
        empty_test = next((t for t in tests if "empty" in t.name), None)
        # With minLength=0, there shouldn't be an empty test marked as invalid
        if empty_test:
            assert empty_test.expected_valid is True or "above" in empty_test.name

    def test_max_length_generates_tests(self, validator: ConstraintValidator):
        """Test maxLength test case generation."""
        tests = validator.generate_test_cases("maxLength", 10, {})

        assert len(tests) >= 3

        # Should have test at exact max
        exact_test = next(t for t in tests if "exact" in t.name)
        assert len(exact_test.value) == 10
        assert exact_test.expected_valid is True

        # Should have test above max
        above_test = next(t for t in tests if "above" in t.name)
        assert len(above_test.value) == 11
        assert above_test.expected_valid is False

    # Pattern Tests

    def test_pattern_generates_valid_tests(self, validator: ConstraintValidator):
        """Test pattern test case generation."""
        pattern = r"^[a-z][a-z0-9-]*$"
        tests = validator.generate_test_cases("pattern", pattern, {})

        assert len(tests) >= 2

        # Should have valid patterns
        valid_tests = [t for t in tests if t.expected_valid]
        assert len(valid_tests) >= 1

        # Should have invalid patterns
        invalid_tests = [t for t in tests if not t.expected_valid]
        assert len(invalid_tests) >= 1

    # Numeric Bounds Tests

    def test_minimum_generates_tests(self, validator: ConstraintValidator):
        """Test minimum test case generation."""
        tests = validator.generate_test_cases("minimum", 10, {})

        assert len(tests) >= 3

        # At minimum should be valid
        exact_test = next(t for t in tests if "exact" in t.name)
        assert exact_test.value == 10
        assert exact_test.expected_valid is True

        # Below minimum should be invalid
        below_test = next(t for t in tests if "below" in t.name)
        assert below_test.value == 9
        assert below_test.expected_valid is False

    def test_maximum_generates_tests(self, validator: ConstraintValidator):
        """Test maximum test case generation."""
        tests = validator.generate_test_cases("maximum", 100, {})

        assert len(tests) >= 3

        # At maximum should be valid
        exact_test = next(t for t in tests if "exact" in t.name)
        assert exact_test.value == 100
        assert exact_test.expected_valid is True

        # Above maximum should be invalid
        above_test = next(t for t in tests if "above" in t.name)
        assert above_test.value == 101
        assert above_test.expected_valid is False

    def test_exclusive_minimum_generates_tests(self, validator: ConstraintValidator):
        """Test exclusiveMinimum test case generation."""
        tests = validator.generate_test_cases("exclusiveMinimum", 0, {})

        # At boundary should be invalid (exclusive)
        exact_test = next(t for t in tests if "exact" in t.name)
        assert exact_test.value == 0
        assert exact_test.expected_valid is False

    def test_exclusive_maximum_generates_tests(self, validator: ConstraintValidator):
        """Test exclusiveMaximum test case generation."""
        tests = validator.generate_test_cases("exclusiveMaximum", 100, {})

        # At boundary should be invalid (exclusive)
        exact_test = next(t for t in tests if "exact" in t.name)
        assert exact_test.value == 100
        assert exact_test.expected_valid is False

    # Array Bounds Tests

    def test_min_items_generates_tests(self, validator: ConstraintValidator):
        """Test minItems test case generation."""
        tests = validator.generate_test_cases("minItems", 2, {})

        assert len(tests) >= 2

        # At min should be valid
        exact_test = next(t for t in tests if "exact" in t.name)
        assert len(exact_test.value) == 2
        assert exact_test.expected_valid is True

        # Below min should be invalid
        below_test = next(t for t in tests if "below" in t.name)
        assert len(below_test.value) == 1
        assert below_test.expected_valid is False

    def test_max_items_generates_tests(self, validator: ConstraintValidator):
        """Test maxItems test case generation."""
        tests = validator.generate_test_cases("maxItems", 5, {})

        assert len(tests) >= 2

        # At max should be valid
        exact_test = next(t for t in tests if "exact" in t.name)
        assert len(exact_test.value) == 5
        assert exact_test.expected_valid is True

        # Above max should be invalid
        above_test = next(t for t in tests if "above" in t.name)
        assert len(above_test.value) == 6
        assert above_test.expected_valid is False

    def test_unique_items_generates_tests(self, validator: ConstraintValidator):
        """Test uniqueItems test case generation."""
        tests = validator.generate_test_cases("uniqueItems", True, {})

        # Should have unique array test
        unique_test = next(t for t in tests if "unique" in t.name and t.expected_valid)
        assert len(unique_test.value) == len(set(unique_test.value))

        # Should have duplicate array test
        dup_test = next(t for t in tests if "duplicate" in t.name)
        assert dup_test.expected_valid is False

    # Enum Tests

    def test_enum_generates_tests(self, validator: ConstraintValidator):
        """Test enum test case generation."""
        enum_values = ["active", "inactive", "pending"]
        tests = validator.generate_test_cases("enum", enum_values, {})

        assert len(tests) >= 2

        # Should have valid enum tests
        valid_tests = [t for t in tests if t.expected_valid]
        assert all(t.value in enum_values for t in valid_tests)

        # Should have invalid enum test
        invalid_test = next(t for t in tests if not t.expected_valid)
        assert invalid_test.value not in enum_values

    # Type Tests

    def test_type_string_generates_tests(self, validator: ConstraintValidator):
        """Test type string test case generation."""
        tests = validator.generate_test_cases("type", "string", {})

        valid_test = next(t for t in tests if t.expected_valid)
        assert isinstance(valid_test.value, str)

        invalid_tests = [t for t in tests if not t.expected_valid]
        assert len(invalid_tests) >= 1

    def test_type_integer_generates_tests(self, validator: ConstraintValidator):
        """Test type integer test case generation."""
        tests = validator.generate_test_cases("type", "integer", {})

        valid_test = next(t for t in tests if t.expected_valid)
        assert isinstance(valid_test.value, int)

    def test_type_boolean_generates_tests(self, validator: ConstraintValidator):
        """Test type boolean test case generation."""
        tests = validator.generate_test_cases("type", "boolean", {})

        valid_test = next(t for t in tests if t.expected_valid)
        assert isinstance(valid_test.value, bool)

    # Required Tests

    def test_required_generates_tests(self, validator: ConstraintValidator):
        """Test required fields test case generation."""
        required_fields = ["name", "email"]
        tests = validator.generate_test_cases("required", required_fields, {})

        assert len(tests) == len(required_fields)

        for field in required_fields:
            field_test = next(t for t in tests if field in t.name)
            assert field_test.expected_valid is False

    # Comparison Tests

    def test_compare_results_no_discrepancy(self, validator: ConstraintValidator):
        """Test comparison when spec and API agree."""
        test_case = ValidationTestCase(
            name="test",
            value="valid",
            expected_valid=True,
        )

        result = validator.compare_results(test_case, api_accepted=True)
        assert result is None

    def test_compare_results_spec_stricter(self, validator: ConstraintValidator):
        """Test comparison when spec is stricter than API."""
        test_case = ValidationTestCase(
            name="test",
            value="invalid_per_spec",
            expected_valid=False,
        )

        result = validator.compare_results(test_case, api_accepted=True)
        assert result is not None
        assert result.discrepancy_type == DiscrepancyType.SPEC_STRICTER

    def test_compare_results_spec_looser(self, validator: ConstraintValidator):
        """Test comparison when spec is looser than API."""
        test_case = ValidationTestCase(
            name="test",
            value="valid_per_spec",
            expected_valid=True,
        )

        result = validator.compare_results(test_case, api_accepted=False)
        assert result is not None
        assert result.discrepancy_type == DiscrepancyType.SPEC_LOOSER


class TestDiscrepancy:
    """Tests for Discrepancy dataclass."""

    def test_discrepancy_creation(self):
        """Test creating a discrepancy."""
        d = Discrepancy(
            path="/test",
            property_name="name",
            constraint_type="minLength",
            discrepancy_type=DiscrepancyType.SPEC_STRICTER,
            spec_value=5,
            api_behavior=3,
        )

        assert d.path == "/test"
        assert d.property_name == "name"
        assert d.constraint_type == "minLength"
        assert d.discrepancy_type == DiscrepancyType.SPEC_STRICTER
        assert d.spec_value == 5
        assert d.api_behavior == 3

    def test_discrepancy_with_test_values(self):
        """Test discrepancy with test values."""
        d = Discrepancy(
            path="/test",
            property_name="count",
            constraint_type="maximum",
            discrepancy_type=DiscrepancyType.SPEC_LOOSER,
            spec_value=100,
            api_behavior=50,
            test_values=[75, 60, 51],
        )

        assert len(d.test_values) == 3
        assert 75 in d.test_values


class TestValidationTestCase:
    """Tests for ValidationTestCase dataclass."""

    def test_testcase_creation(self):
        """Test creating a test case."""
        tc = ValidationTestCase(
            name="minLength_exact_5",
            value="aaaaa",
            expected_valid=True,
            description="String of exactly 5 characters",
        )

        assert tc.name == "minLength_exact_5"
        assert tc.value == "aaaaa"
        assert tc.expected_valid is True
        assert tc.description == "String of exactly 5 characters"
