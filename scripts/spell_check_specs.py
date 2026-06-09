"""Check spelling in OpenAPI spec text fields using codespell."""

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
    for spec_file in spec_files:
        with spec_file.open() as fh:
            spec = json.load(fh)
        all_text.extend(_extract_text(spec))

    console.print(f"  Extracted {len(all_text)} text fields from {len(spec_files)} specs")

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

    if result.stdout.strip():
        console.print(f"\n[red]Found spelling errors:[/red]\n{result.stdout}")
        return 1

    console.print("[green]No spelling errors found in spec text fields.[/green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
