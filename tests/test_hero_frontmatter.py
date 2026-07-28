"""Translated hero blocks must keep their structural fields (#736).

``docs/*/index.mdx`` carries a Starlight ``hero`` block whose ``actions`` mix two kinds of field::

    hero:
      actions:
        - text: Pipeline Overview      # user-visible -- must be translated
          link: 02-pipeline/           # structural -- must NOT be translated
          icon: right-arrow            # structural
          variant: primary             # structural

Translating a structural field breaks the page: a translated ``link`` is a dead route, and a
translated ``icon`` or ``variant`` is not a value Starlight recognises.

This has gone wrong before. #420 and #427 were filed the same day with the identical body
"Retranslation with fixed translator.", and #428 was the retranslation that repaired the hero
fields. Nothing prevented a recurrence: the fleet translation audit reports only ``MISSING`` and
``STALE``, comparing a stored ``sourceHash`` against the English file without ever looking inside
the frontmatter. A future retranslation could turn ``link: 02-pipeline/`` into twelve dead links
with every check still green -- greener, in fact, since the ``sourceHash`` would be correctly
restamped.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
SOURCE_LOCALE = "en"

# Not a locale: `superpowers` is skill documentation that sits alongside the translated tree.
NON_LOCALE_DIRS = {"superpowers"}

# Fields that identify or configure the action rather than describe it to a reader.
STRUCTURAL_FIELDS = ("link", "icon", "variant")

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)


def _frontmatter(path: Path) -> dict:
    match = FRONTMATTER.match(path.read_text())
    assert match, f"{path} has no YAML frontmatter"
    return yaml.safe_load(match.group(1)) or {}


def _locales() -> list[str]:
    return sorted(
        p.name
        for p in DOCS.iterdir()
        if p.is_dir() and p.name != SOURCE_LOCALE and p.name not in NON_LOCALE_DIRS
        if (p / "index.mdx").exists()
    )


@pytest.fixture(scope="module")
def english_hero() -> dict:
    return _frontmatter(DOCS / SOURCE_LOCALE / "index.mdx")["hero"]


def test_locale_discovery_is_not_vacuous():
    """A discovery bug that finds no locales would make every other test here pass silently."""
    locales = _locales()
    assert len(locales) >= 10, f"expected the full translated set, found {locales}"
    assert SOURCE_LOCALE not in locales


@pytest.mark.parametrize("locale", _locales())
def test_structural_fields_are_identical_to_english(locale, english_hero):
    """``link``, ``icon`` and ``variant`` must survive translation byte-for-byte."""
    translated = _frontmatter(DOCS / locale / "index.mdx")["hero"]
    en_actions = english_hero["actions"]
    actions = translated["actions"]

    assert len(actions) == len(en_actions), (
        f"{locale}: hero has {len(actions)} actions, English has {len(en_actions)}"
    )

    for index, (action, en_action) in enumerate(zip(actions, en_actions, strict=True)):
        for field in STRUCTURAL_FIELDS:
            assert action.get(field) == en_action.get(field), (
                f"{locale}: hero.actions[{index}].{field} is "
                f"{action.get(field)!r}, expected {en_action.get(field)!r}. "
                "Structural fields must not be translated -- a translated link is a dead "
                "route, and a translated icon or variant is not a value Starlight knows."
            )


@pytest.mark.parametrize("locale", _locales())
def test_visible_text_is_actually_translated(locale, english_hero):
    """The flip side: leaving ``text`` in English means the action was skipped, not translated."""
    translated = _frontmatter(DOCS / locale / "index.mdx")["hero"]

    for index, (action, en_action) in enumerate(
        zip(translated["actions"], english_hero["actions"], strict=True)
    ):
        assert action["text"] != en_action["text"], (
            f"{locale}: hero.actions[{index}].text is still the English {en_action['text']!r}"
        )

    assert translated.get("tagline"), f"{locale}: hero.tagline is missing"
    assert translated["tagline"] != english_hero["tagline"], (
        f"{locale}: hero.tagline is still the English text"
    )
