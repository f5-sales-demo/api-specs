"""Native map constraints retain scope and preserve vendor evidence."""

import copy

import pytest
from jsonschema import Draft4Validator

from scripts.transform import TransformConfig, project_native_map_constraints
from scripts.utils.map_constraints import project_map

PREFIX = "ves.io.schema.rules.map."


def test_custom_errors_native_bounds_and_idempotence():
    rules = {
        PREFIX + "max_pairs": "16",
        PREFIX + "values.string.max_len": "65536",
        PREFIX + "keys.uint32.ranges": "3,4,5,300-599",
        PREFIX + "values.string.uri_ref": "true",
    }
    node = {"type": "object", "x-ves-validation-rules": rules}
    spec = {"components": {"schemas": {"Root": {"properties": {"custom_errors": node}}}}}
    project_native_map_constraints(spec, TransformConfig(), "synthetic.json")
    assert node["additionalProperties"] == {"type": "string", "maxLength": 65536}
    assert node["maxProperties"] == 16
    assert node["x-ves-validation-rules"] == rules
    assert "propertyNames" not in node
    assert "uniqueItems" not in node
    assert Draft4Validator(node).is_valid({"300": "string:///" + "a" * 65526})
    assert not Draft4Validator(node).is_valid({"300": "string:///" + "a" * 65527})
    before = copy.deepcopy(spec)
    project_native_map_constraints(spec, TransformConfig(), "synthetic.json")
    assert spec == before


@pytest.mark.parametrize("value_schema", [{"type": "integer"}, {"type": "object"}, False])
def test_incompatible_values_stay_extension_only(value_schema):
    node = {
        "type": "object",
        "additionalProperties": value_schema,
        "x-ves-validation-rules": {PREFIX + "values.string.max_len": "10"},
    }
    assert project_map(node, {})
    assert node["additionalProperties"] == value_schema


def test_conflicting_native_values_fail():
    node = {
        "type": "object",
        "additionalProperties": {"type": "string", "maxLength": 9},
        "x-ves-validation-rules": {PREFIX + "values.string.max_len": "10"},
    }
    with pytest.raises(ValueError, match="Conflicting native"):
        project_map(node, {})
