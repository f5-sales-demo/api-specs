"""Check spelling in OpenAPI spec text fields and property names using codespell."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console

console = Console()

SPEC_DIR = Path("release/specs")
TEXT_FIELDS = ("description", "summary", "title")
_MIN_TEXT_LENGTH = 5


def _extract_text(obj: Any) -> list[str]:
    """Recursively extract all text field values from an OpenAPI spec."""
    texts: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in TEXT_FIELDS and isinstance(value, str) and len(value) > _MIN_TEXT_LENGTH:
                texts.append(value)
            texts.extend(_extract_text(value))
    elif isinstance(obj, list):
        for item in obj:
            texts.extend(_extract_text(item))
    return texts


def _extract_property_names(obj: Any) -> set[str]:
    """Recursively extract all schema property names from an OpenAPI spec."""
    names: set[str] = set()
    if isinstance(obj, dict):
        if "properties" in obj and isinstance(obj["properties"], dict):
            names.update(obj["properties"].keys())
        for value in obj.values():
            names.update(_extract_property_names(value))
    elif isinstance(obj, list):
        for item in obj:
            names.update(_extract_property_names(item))
    return names


def _load_known_property_corrections() -> set[str]:
    """Load property names already tracked in property_name_corrections.yaml."""
    config_path = Path("config/property_name_corrections.yaml")
    if not config_path.exists():
        return set()
    with config_path.open() as fh:
        cfg = yaml.safe_load(fh) or {}
    return {c["old_key"] for c in cfg.get("corrections", [])}


def _load_false_positives() -> list[str]:
    """Load false-positive words from spelling_corrections.yaml."""
    config_path = Path("config/spelling_corrections.yaml")
    if not config_path.exists():
        return []
    with config_path.open() as fh:
        cfg = yaml.safe_load(fh) or {}
    return cfg.get("false_positives", [])


def main() -> int:
    """Extract text from specs and run codespell."""
    console.print("[bold blue]Spell-checking spec text fields[/bold blue]")

    spec_files = sorted(SPEC_DIR.glob("*.json"))
    if not spec_files:
        console.print("[yellow]No spec files found in release/specs/[/yellow]")
        return 0

    all_text: list[str] = []
    all_property_names: set[str] = set()
    for spec_file in spec_files:
        with spec_file.open() as fh:
            spec = json.load(fh)
        all_text.extend(_extract_text(spec))
        all_property_names.update(_extract_property_names(spec))

    console.print(f"  Extracted {len(all_text)} text fields from {len(spec_files)} specs")
    console.print(f"  Found {len(all_property_names)} unique property names")

    false_positives = _load_false_positives()

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False
    ) as tmp:
        tmp.write("\n".join(all_text))
        tmp_path = tmp.name

    cmd = ["codespell", tmp_path]
    if false_positives:
        cmd.extend(["--ignore-words-list", ",".join(false_positives)])

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603

    Path(tmp_path).unlink(missing_ok=True)

    errors_found = False
    if result.stdout.strip():
        console.print(f"\n[red]Found spelling errors in text fields:[/red]\n{result.stdout}")
        errors_found = True
    else:
        console.print("[green]No spelling errors in text fields.[/green]")

    known_corrections = _load_known_property_corrections()
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False
    ) as tmp2:
        tmp2.write("\n".join(all_property_names))
        prop_path = tmp2.name

    prop_cmd = ["codespell", prop_path]
    if false_positives:
        prop_cmd.extend(["--ignore-words-list", ",".join(false_positives)])
    prop_result = subprocess.run(  # noqa: S603
        prop_cmd, capture_output=True, text=True, check=False
    )
    Path(prop_path).unlink(missing_ok=True)

    if prop_result.stdout.strip():
        new_findings = []
        for line in prop_result.stdout.strip().split("\n"):
            typo = line.split(":")[1].strip().split(" ")[0] if ":" in line else ""
            if typo and typo not in known_corrections:
                new_findings.append(line)

        if new_findings:
            console.print(
                f"\n[red]Found {len(new_findings)} misspelled property names "
                f"not yet tracked:[/red]"
            )
            for finding in new_findings:
                console.print(f"  {finding}")
            errors_found = True
        else:
            console.print("[green]All misspelled property names are tracked.[/green]")
    else:
        console.print("[green]No spelling errors in property names.[/green]")

    return 1 if errors_found else 0


if __name__ == "__main__":
    sys.exit(main())
