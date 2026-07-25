"""Guard tests for dangling ``$ref`` targets in the published specs.

A ``$ref`` that points at a schema no spec defines is fatal to every consumer:
Spectral rejects it with ``invalid-ref``, which is an error, and the quality
gate is configured for zero errors. One such reference in the upstream input
therefore stops the release job, and no artifact publishes at all.

Two invariants are enforced here:

1. The published ``release/specs`` artifact contains no dangling ``$ref``.
   The failure names the file, the JSON pointer and the missing schema, so a
   regression is diagnosable from the assertion alone rather than from a
   Spectral error count.
2. ``fix_dangling_refs`` applies the corrections declared in
   ``config/dangling_ref_corrections.yaml`` and only those: it never
   overwrites a definition upstream already ships, never injects a schema
   nothing references, and rejects an unknown remedy instead of silently
   doing nothing.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.transform import (
    TransformConfig,
    find_dangling_refs,
    fix_dangling_refs,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_SPECS_DIR = REPO_ROOT / "release" / "specs"
DANGLING_REF_CONFIG = REPO_ROOT / "config" / "dangling_ref_corrections.yaml"

# The upstream defect this guard was written for: F5 emits the
# ``k8s_cluster_status`` member of ``BotInfraDeploymentDetails`` but never
# emits the message it points at.
UPSTREAM_DEFECT_SCHEMA = "threat_intelligenceK8sClusterDeliveryStatus"
UPSTREAM_DEFECT_FILE = (
    "docs-cloud-f5-com.0038.public.ves.io.schema.shape.bot_defense"
    ".threat_intelligence.bot_detection_rule.ves-swagger.json"
)

# Maximum number of offending references quoted in an assertion message.
_MAX_REPORTED = 20


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def corrections() -> list[dict[str, Any]]:
    with DANGLING_REF_CONFIG.open() as fh:
        raw = yaml.safe_load(fh) or {}
    return raw.get("corrections", [])


@pytest.fixture(scope="module")
def config(corrections: list[dict[str, Any]]) -> TransformConfig:
    return TransformConfig(metadata={"dangling_ref_corrections": corrections})


@pytest.fixture
def defective_spec() -> dict:
    """A spec shaped like the upstream defect: a member with no definition."""
    return {
        "openapi": "3.0.0",
        "info": {"title": "Test", "version": ""},
        "paths": {},
        "components": {
            "schemas": {
                "threat_intelligenceBotInfraDeploymentDetails": {
                    "type": "object",
                    "x-ves-oneof-field-status": (
                        '["bot_infra_status","k8s_cluster_status","region_status"]'
                    ),
                    "properties": {
                        "k8s_cluster_status": {
                            "$ref": f"#/components/schemas/{UPSTREAM_DEFECT_SCHEMA}"
                        },
                    },
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# find_dangling_refs
# ---------------------------------------------------------------------------


class TestFindDanglingRefs:
    def test_reports_pointer_and_schema_name(self, defective_spec: dict) -> None:
        assert find_dangling_refs(defective_spec) == [
            (
                "components.schemas.threat_intelligenceBotInfraDeploymentDetails"
                ".properties.k8s_cluster_status.$ref",
                UPSTREAM_DEFECT_SCHEMA,
            )
        ]

    def test_resolvable_ref_is_not_reported(self, defective_spec: dict) -> None:
        defective_spec["components"]["schemas"][UPSTREAM_DEFECT_SCHEMA] = {"type": "object"}
        assert not find_dangling_refs(defective_spec)

    def test_spec_without_components_is_clean(self) -> None:
        assert not find_dangling_refs({"openapi": "3.0.0", "paths": {}})

    def test_external_ref_is_ignored(self) -> None:
        spec = {"components": {"schemas": {}}, "paths": {"/a": {"$ref": "other.json"}}}
        assert not find_dangling_refs(spec)


# ---------------------------------------------------------------------------
# fix_dangling_refs
# ---------------------------------------------------------------------------


class TestFixDanglingRefs:
    def test_upstream_defect_is_stubbed(
        self, defective_spec: dict, config: TransformConfig
    ) -> None:
        fixed = fix_dangling_refs(defective_spec, config, UPSTREAM_DEFECT_FILE)

        assert not find_dangling_refs(fixed)
        stub = fixed["components"]["schemas"][UPSTREAM_DEFECT_SCHEMA]
        assert stub["type"] == "object"
        # A stub must not invent API surface.
        assert "properties" not in stub

    def test_existing_definition_is_never_overwritten(
        self, defective_spec: dict, config: TransformConfig
    ) -> None:
        upstream = {"type": "object", "properties": {"cluster": {"type": "string"}}}
        defective_spec["components"]["schemas"][UPSTREAM_DEFECT_SCHEMA] = copy.deepcopy(upstream)

        fixed = fix_dangling_refs(defective_spec, config, UPSTREAM_DEFECT_FILE)

        assert fixed["components"]["schemas"][UPSTREAM_DEFECT_SCHEMA] == upstream

    def test_other_files_are_untouched(self, defective_spec: dict, config: TransformConfig) -> None:
        fixed = fix_dangling_refs(defective_spec, config, "some.other.spec.json")

        assert UPSTREAM_DEFECT_SCHEMA not in fixed["components"]["schemas"]

    def test_unreferenced_schema_is_not_injected(self, config: TransformConfig) -> None:
        spec = {"components": {"schemas": {"other": {"type": "object"}}}}

        fixed = fix_dangling_refs(spec, config, UPSTREAM_DEFECT_FILE)

        assert UPSTREAM_DEFECT_SCHEMA not in fixed["components"]["schemas"]

    def test_unknown_remedy_is_rejected(self, defective_spec: dict) -> None:
        config = TransformConfig(
            metadata={
                "dangling_ref_corrections": [
                    {"schema": UPSTREAM_DEFECT_SCHEMA, "remedy": "wish-it-away"}
                ]
            }
        )

        with pytest.raises(ValueError, match="wish-it-away"):
            fix_dangling_refs(defective_spec, config, UPSTREAM_DEFECT_FILE)

    def test_stub_without_a_definition_is_rejected(self, defective_spec: dict) -> None:
        config = TransformConfig(
            metadata={
                "dangling_ref_corrections": [{"schema": UPSTREAM_DEFECT_SCHEMA, "remedy": "stub"}]
            }
        )

        with pytest.raises(ValueError, match="without a definition"):
            fix_dangling_refs(defective_spec, config, UPSTREAM_DEFECT_FILE)

    def test_no_corrections_configured_is_a_noop(self, defective_spec: dict) -> None:
        before = copy.deepcopy(defective_spec)

        fixed = fix_dangling_refs(defective_spec, TransformConfig(), UPSTREAM_DEFECT_FILE)

        assert fixed == before


# ---------------------------------------------------------------------------
# Correction configuration
# ---------------------------------------------------------------------------


class TestCorrectionConfig:
    def test_upstream_defect_is_tracked(self, corrections: list[dict]) -> None:
        assert [c["schema"] for c in corrections].count(UPSTREAM_DEFECT_SCHEMA) == 1

    def test_every_correction_is_justified(self, corrections: list[dict]) -> None:
        for rule in corrections:
            schema = rule.get("schema")
            assert schema, f"correction without a schema: {rule}"
            assert rule.get("remedy") == "stub", f"{schema}: unsupported remedy"
            assert rule.get("api_status") == "upstream_defect", (
                f"{schema}: corrections must record why upstream is wrong"
            )
            assert rule.get("notes"), f"{schema}: missing rationale"
            assert rule.get("referenced_by"), f"{schema}: missing referencing member"
            definition = rule.get("definition") or {}
            assert definition.get("type") == "object", f"{schema}: stub must be typed"
            assert "properties" not in definition, f"{schema}: a stub must not invent API surface"
            assert definition.get("x-ves-proto-message"), (
                f"{schema}: stub must name the upstream proto message"
            )


# ---------------------------------------------------------------------------
# Published artifact
# ---------------------------------------------------------------------------


class TestPublishedSpecs:
    def test_published_specs_have_no_dangling_refs(self) -> None:
        spec_files = sorted(RELEASE_SPECS_DIR.glob("*.json"))
        assert spec_files, f"No published specs found in {RELEASE_SPECS_DIR}"

        offenders: list[str] = []
        for spec_file in spec_files:
            with spec_file.open() as fh:
                spec = json.load(fh)
            offenders.extend(
                f"{spec_file.name}: {pointer} -> {schema}"
                for pointer, schema in find_dangling_refs(spec)
            )

        assert not offenders, (
            f"{len(offenders)} dangling $ref(s) in the published specs; "
            "Spectral rejects each one as an invalid-ref error and the release "
            "gate allows zero errors. Add a correction to "
            "config/dangling_ref_corrections.yaml:\n  " + "\n  ".join(offenders[:_MAX_REPORTED])
        )
