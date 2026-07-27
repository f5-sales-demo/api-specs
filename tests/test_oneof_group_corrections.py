"""Misspelled ``x-ves-oneof-field-*`` group keys must be corrected (#690).

F5 groups mutually-exclusive properties with a vendor extension whose **key** carries the group
name::

    "x-ves-oneof-field-lb_source_ip_persistance_choice": "[\\"disable_...\\",\\"enable_...\\"]"

``fix_property_names`` corrects the property names listed in the *value*, but the group name lives
in the extension **key**, which that transform does not touch. After #711 regenerated the artifact,
this key became the only surviving ``..._persistance_choice`` in the published specs -- and its own
now reads ``persistence``, so the key contradicts its contents.

The group name is not cosmetic. ``api-specs-enriched``'s ``compile_catalog.py`` turns it into a JSON
object key in the published ``api-catalog.json``, which ``xcsh`` renders verbatim in the "OneOf
Groups" table a user reads. That particular group is currently below the catalog's ``max_depth``
walk limit, so the typo is latent rather than visible -- one depth bump away from surfacing.

Unlike a property rename, this carries no wire risk: the extension is metadata describing a schema,
never a field sent to or returned by the API. The wire keys are the properties, and they are
untouched here.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import yaml

from scripts.transform import TransformConfig, fix_oneof_group_names

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_SPECS_DIR = REPO_ROOT / "release" / "specs"
CONFIG_PATH = REPO_ROOT / "config" / "oneof_group_corrections.yaml"
PREFIX = "x-ves-oneof-field-"

CORRECTIONS = yaml.safe_load(CONFIG_PATH.read_text())["corrections"]


def _published_group_keys() -> dict[str, list[str]]:
    """Return ``{group key: [filenames]}`` for every ``x-ves-oneof-field-*`` in the artifact."""
    found: dict[str, list[str]] = {}
    for path in sorted(RELEASE_SPECS_DIR.glob("*.json")):
        if path.name.startswith("."):
            continue
        for key in set(re.findall(rf'"({re.escape(PREFIX)}[^"]+)"', path.read_text())):
            found.setdefault(key, []).append(path.name)
    return found


class TestConfig:
    def test_every_entry_is_fully_specified(self):
        assert CORRECTIONS, "config declares no corrections"
        for c in CORRECTIONS:
            for field in ("old_group", "new_group", "reason"):
                assert c.get(field), f"{c} is missing {field!r}"
            assert c["old_group"] != c["new_group"]

    def test_no_entry_is_dead_config(self):
        """A correction whose old group is absent from the artifact is stale and must be removed."""
        published = _published_group_keys()
        for c in CORRECTIONS:
            old = PREFIX + c["old_group"]
            new = PREFIX + c["new_group"]
            assert old in published or new in published, (
                f"neither {old} nor {new} appears in release/specs -- stale config entry"
            )


class TestTransform:
    def test_renames_the_group_key_in_place(self):
        spec = {
            "components": {
                "schemas": {
                    "Thing": {
                        f"{PREFIX}lb_source_ip_persistance_choice": '["a","b"]',
                        "properties": {"a": {}, "b": {}},
                    }
                }
            }
        }
        out = fix_oneof_group_names(copy.deepcopy(spec), _config(), "t.json")
        schema = out["components"]["schemas"]["Thing"]

        assert f"{PREFIX}lb_source_ip_persistence_choice" in schema
        assert f"{PREFIX}lb_source_ip_persistance_choice" not in schema

    def test_the_value_is_carried_over_untouched(self):
        spec = {
            "components": {
                "schemas": {"Thing": {f"{PREFIX}lb_source_ip_persistance_choice": '["x","y"]'}}
            }
        }
        out = fix_oneof_group_names(copy.deepcopy(spec), _config(), "t.json")
        assert (
            out["components"]["schemas"]["Thing"][f"{PREFIX}lb_source_ip_persistence_choice"]
            == '["x","y"]'
        )

    def test_unrelated_oneof_groups_are_untouched(self):
        spec = {"components": {"schemas": {"T": {f"{PREFIX}waf_filter_choice": '["p","q"]'}}}}
        out = fix_oneof_group_names(copy.deepcopy(spec), _config(), "t.json")
        assert f"{PREFIX}waf_filter_choice" in out["components"]["schemas"]["T"]

    def test_properties_are_never_touched(self):
        """The wire contract lives in the properties; only the extension key may move."""
        spec = {
            "components": {
                "schemas": {
                    "Thing": {
                        f"{PREFIX}lb_source_ip_persistance_choice": '["a"]',
                        "properties": {"disable_lb_source_ip_persistance": {"type": "object"}},
                    }
                }
            }
        }
        out = fix_oneof_group_names(copy.deepcopy(spec), _config(), "t.json")
        props = out["components"]["schemas"]["Thing"]["properties"]
        assert "disable_lb_source_ip_persistance" in props, "a property name must not be renamed"

    def test_is_idempotent(self):
        spec = {
            "components": {"schemas": {"T": {f"{PREFIX}lb_source_ip_persistance_choice": '["a"]'}}}
        }
        once = fix_oneof_group_names(copy.deepcopy(spec), _config(), "t.json")
        twice = fix_oneof_group_names(copy.deepcopy(once), _config(), "t.json")
        assert json.dumps(once, sort_keys=True) == json.dumps(twice, sort_keys=True)


class TestPublishedArtifact:
    def test_no_configured_typo_survives_in_the_artifact(self):
        published = _published_group_keys()
        offenders = {
            PREFIX + c["old_group"]: published[PREFIX + c["old_group"]]
            for c in CORRECTIONS
            if PREFIX + c["old_group"] in published
        }
        assert not offenders, (
            f"misspelled oneof group keys still published: "
            f"{ {k: len(v) for k, v in offenders.items()} }"
        )

    def test_the_corrected_group_is_present(self):
        published = _published_group_keys()
        for c in CORRECTIONS:
            assert PREFIX + c["new_group"] in published

    def test_key_and_value_agree(self):
        """The key's group name must not contradict the property names in its own value."""
        for c in CORRECTIONS:
            new_key = PREFIX + c["new_group"]
            for path in sorted(RELEASE_SPECS_DIR.glob("*.json")):
                spec = json.loads(path.read_text())
                for schema in spec.get("components", {}).get("schemas", {}).values():
                    if not isinstance(schema, dict) or new_key not in schema:
                        continue
                    assert c["old_group"].split("_")[-2] not in str(schema[new_key]), (
                        f"{path.name}: {new_key} still lists a misspelled variant"
                    )


def _config() -> TransformConfig:
    return TransformConfig(metadata={"oneof_group_corrections": CORRECTIONS})


def test_no_group_key_carries_a_known_misspelling():
    """#690 scope item 3, as a standing audit rather than a one-off finding.

    Every misspelling this repository already knows about lives in
    ``property_name_corrections.yaml`` as the part of an ``old_key`` that its ``new_key`` drops.
    If any of those tokens appears in a published group key, that key is misspelled too and needs
    an entry here.

    Written to keep working as configuration grows: adding a legitimate correction does not fail
    it, and a *new* upstream typo of an already-known word does. The original audit also ran
    codespell over all 682 distinct group names, which found the same single offender.
    """
    property_corrections = yaml.safe_load(
        (REPO_ROOT / "config" / "property_name_corrections.yaml").read_text()
    )["corrections"]

    known_typos = set()
    for c in property_corrections:
        old_tokens = set(re.split(r"[_\-]", c["old_key"]))
        new_tokens = set(re.split(r"[_\-]", c["new_key"]))
        known_typos |= old_tokens - new_tokens

    covered = {c["old_group"] for c in CORRECTIONS}
    offenders = {
        key: sorted(t for t in known_typos if t in key)
        for key in _published_group_keys()
        if key.removeprefix(PREFIX) not in covered and any(t in key for t in known_typos)
    }

    assert not offenders, (
        "published oneof group keys contain a misspelling this repo already corrects "
        f"elsewhere, with no entry in {CONFIG_PATH.name}: {offenders}"
    )
