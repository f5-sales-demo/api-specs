"""Guard that the committed ``release/specs`` artifact is current.

``release/specs`` is a build product that is also committed, and nothing commits it back
automatically. Corrections therefore land in ``config/``, the transform applies them correctly, and
the published artifact keeps whatever the last manual run produced (#681).

The existing correction suites cannot catch that. ``test_property_name_corrections`` and
``test_spelling_corrections`` read the committed artifact as *input*, transform it in memory, and
assert on the *result* -- which proves the transform is correct while an arbitrarily stale artifact
stays green. This module asserts the complementary property: running the transform pipeline over the
committed artifact is a **no-op**.

Scope is deliberately narrower than "reproduce the pipeline". ``specs/original`` is untracked and
downloaded from a mutable upstream URL, so comparing against a fresh download would need network and
F5 credentials in CI and would go red whenever F5 publishes a new drop -- new input, not our drift.
Idempotence over the committed artifact needs neither, and avoids every nondeterminism source in the
pipeline: unseeded Hypothesis in ``validate`` feeding ``reconcile``, the silent dry-run fallback when
no token is present, and ``CHANGELOG.md`` ordering from an unsorted glob.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path

import pytest
import yaml

from scripts.transform import SpecTransformer, load_config, load_spec_metadata
from scripts.utils.spec_loader import save_spec_to_file

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "validation.yaml"
RELEASE_SPECS_DIR = REPO_ROOT / "release" / "specs"
ARTIFACT_METADATA = RELEASE_SPECS_DIR / ".spec_metadata.json"


def _artifact_config():
    """Return a ``TransformConfig`` that reads the committed artifact.

    ``info.version`` is stamped by ``inject_info_version`` from ``config.metadata["spec_date"]``,
    which ``load_config`` normally takes from ``specs/original/.spec_metadata.json`` -- a record of
    whatever was last downloaded on this machine, unrelated to what the artifact was built from. A
    developer with a stale download would otherwise see all 283 specs "drift" on the version line
    alone.

    The artifact carries its own provenance in ``release/specs/.spec_metadata.json``; that is the
    correct source here, and it keeps the comparison total rather than excluding a field.
    """
    config = load_config(CONFIG_PATH)
    config.input_dir = str(RELEASE_SPECS_DIR)
    config.metadata.update(load_spec_metadata(RELEASE_SPECS_DIR))
    return config


@pytest.fixture(scope="module")
def drift(tmp_path_factory) -> list[tuple[str, str, str]]:
    """Return ``(filename, committed, regenerated)`` for every spec the transform would change.

    Serialisation goes through ``save_spec_to_file`` rather than a hand-rolled ``json.dumps`` so the
    comparison uses exactly the bytes the pipeline would write, including ``_compact_short_arrays``.
    """
    scratch_dir = tmp_path_factory.mktemp("regenerated")
    config = _artifact_config()
    transformer = SpecTransformer(config)

    drifted: list[tuple[str, str, str]] = []
    for result in transformer.transform_all():
        committed = (RELEASE_SPECS_DIR / result.filename).read_text()

        scratch = scratch_dir / result.filename
        save_spec_to_file(result.spec, scratch, "json")
        regenerated = scratch.read_text()

        if committed != regenerated:
            drifted.append((result.filename, committed, regenerated))

    return drifted


def test_provenance_comes_from_the_directory_the_specs_came_from(tmp_path):
    """``load_spec_metadata`` must read the drop record next to the specs being transformed.

    Regression guard for the defect this change fixes: ``load_config`` reads metadata from the
    *configured* input dir, so a run over ``release/specs`` used to keep ``specs/original``'s
    metadata and stamp that unrelated download's date into ``info.version`` for every published
    spec.
    """
    (tmp_path / ".spec_metadata.json").write_text(json.dumps({"spec_date": "1999.12.31"}))

    assert load_spec_metadata(tmp_path)["spec_date"] == "1999.12.31"
    assert load_spec_metadata(tmp_path / "nonexistent") == {}, (
        "a missing record must yield {} so info.version is left unset rather than guessed"
    )


def test_the_gate_pins_version_to_the_artifact_not_the_local_download():
    """The gate's config must carry the artifact's own spec_date."""
    artifact_date = json.loads(ARTIFACT_METADATA.read_text())["spec_date"]
    assert _artifact_config().metadata["spec_date"] == artifact_date


def test_artifact_metadata_exists():
    """The artifact must record the drop it was built from, or the gate cannot pin the version."""
    assert ARTIFACT_METADATA.exists(), (
        f"{ARTIFACT_METADATA} is missing -- the gate needs the artifact's own spec_date to stamp "
        "info.version, and must not fall back to the local specs/original download state."
    )
    assert "spec_date" in json.loads(ARTIFACT_METADATA.read_text())


def test_the_guard_is_not_vacuous():
    """A gate that silently inspects nothing would pass forever."""
    config = _artifact_config()

    specs = [p for p in RELEASE_SPECS_DIR.glob("*.json") if not p.name.startswith(".")]
    assert len(specs) > 200, f"expected the full published set, found {len(specs)}"

    enabled = [name for name, on in config.transforms.items() if on]
    assert len(enabled) > 10, f"expected the full transform pipeline, found {enabled}"


def test_every_configured_correction_is_present_in_the_artifact():
    """Each configured rename must actually be presented by the published specs.

    The idempotence check alone is a ratchet: once a key has been renamed, deleting or editing the
    rule leaves the renamed key in place and the transform no-ops, so the gate stays green. This
    asserts the artifact positively reflects the *current* rule set, which catches an edited
    ``new_key``. (A fully deleted rule is still invisible here -- only a rebuild from upstream can
    see that, which is what the scheduled regeneration job does.)
    """
    corrections = yaml.safe_load(
        (REPO_ROOT / "config" / "property_name_corrections.yaml").read_text()
    )["corrections"]

    blob = "\n".join(
        p.read_text() for p in RELEASE_SPECS_DIR.glob("*.json") if not p.name.startswith(".")
    )

    missing = [
        c["new_key"]
        for c in corrections
        if f'"{c["old_key"]}"' in blob and f'"{c["new_key"]}"' not in blob
    ]
    assert not missing, (
        "these corrections are configured, and their old key still appears in the published "
        f"artifact, but the corrected name never does: {missing}"
    )


def test_release_specs_matches_a_fresh_transform(drift):
    """The committed artifact must already be what the transform pipeline produces.

    A failure here means a correction is configured but unpublished: downstream consumers are still
    being served the upstream defect this repository exists to fix.
    """
    if not drift:
        return

    report = "\n".join(f"  {name}" for name, _, _ in drift)
    sample_name, committed, regenerated = drift[0]
    sample = "\n".join(
        f"    {tag} {line}" for tag, line in _first_differing_lines(committed, regenerated, limit=6)
    )

    pytest.fail(
        f"{len(drift)} published spec(s) differ from a fresh transform -- corrections are "
        f"configured but have not reached release/specs (#681):\n"
        f"{report}\n\n"
        f"  first difference, in {sample_name}:\n{sample}\n\n"
        f"  Regenerate the artifact and commit the result."
    )


def _first_differing_lines(committed: str, regenerated: str, limit: int) -> list[tuple[str, str]]:
    """Return up to *limit* ``(marker, line)`` pairs showing where the two texts diverge."""
    diff = difflib.unified_diff(committed.splitlines(), regenerated.splitlines(), lineterm="", n=0)
    out: list[tuple[str, str]] = []
    for line in diff:
        if line.startswith(("---", "+++", "@@")):
            continue
        out.append((line[0], line[1:].strip()))
        if len(out) >= limit:
            break
    return out
