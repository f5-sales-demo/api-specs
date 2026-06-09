"""Verify misspelled property names against the live F5 XC API.

Probes API endpoints to determine whether the live API uses the misspelled
property name (meaning the spec is correct) or the corrected name (meaning
the spec should be fixed).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.table import Table

from .utils.auth import F5XCAuth, load_auth_from_config

console = Console()

CONFIG_PATH = Path("config/property_name_corrections.yaml")
VALIDATION_CONFIG_PATH = Path("config/validation.yaml")

HTTP_OK = 200


def _search_keys(obj: Any, target_key: str) -> bool:
    """Recursively search for a key name anywhere in a JSON structure."""
    if isinstance(obj, dict):
        if target_key in obj:
            return True
        return any(_search_keys(v, target_key) for v in obj.values())
    if isinstance(obj, list):
        return any(_search_keys(item, target_key) for item in obj)
    return False


def _probe_correction(
    auth: F5XCAuth,
    correction: dict,
) -> dict:
    """Probe the live API for a single property name correction.

    Returns a result dict with keys: old_key, new_key, api_has_old,
    api_has_new, status, recommendation.
    """
    old_key = correction["old_key"]
    new_key = correction["new_key"]
    endpoint = correction["probe_endpoint"]
    method = correction.get("probe_method", "GET")

    result = {
        "schema": correction["schema"],
        "old_key": old_key,
        "new_key": new_key,
        "endpoint": endpoint,
        "status": "unknown",
        "api_has_old": False,
        "api_has_new": False,
        "recommendation": "",
    }

    try:
        response = auth.request(method, endpoint)

        if response.status_code != HTTP_OK:
            result["status"] = f"http_{response.status_code}"
            result["recommendation"] = (
                f"API returned {response.status_code} — cannot verify, "
                "needs manual check or different endpoint"
            )
            return result

        data = response.json()
        has_old = _search_keys(data, old_key)
        has_new = _search_keys(data, new_key)

        result["api_has_old"] = has_old
        result["api_has_new"] = has_new

        if has_new and not has_old:
            result["status"] = "fix_spec"
            result["recommendation"] = (
                f"API uses '{new_key}' — spec should be corrected"
            )
        elif has_old and not has_new:
            result["status"] = "upstream_typo"
            result["recommendation"] = (
                f"API uses '{old_key}' — typo is upstream, spec is correct as-is"
            )
        elif has_old and has_new:
            result["status"] = "both_present"
            result["recommendation"] = (
                "API returns both key forms — needs manual investigation"
            )
        else:
            result["status"] = "neither_found"
            result["recommendation"] = (
                "Neither key found in response — endpoint may not "
                "return this schema, try a different probe"
            )

    except Exception as e:  # pylint: disable=broad-exception-caught
        result["status"] = "error"
        result["recommendation"] = f"Probe failed: {e}"

    return result


def _update_config(corrections: list[dict], results: list[dict]) -> bool:
    """Update the corrections config file with verification results."""
    changed = False
    for result in results:
        if result["status"] != "fix_spec":
            continue
        for correction in corrections:
            if (
                correction["schema"] == result["schema"]
                and correction["old_key"] == result["old_key"]
                and not correction.get("verified", False)
            ):
                correction["verified"] = True
                changed = True
    return changed


def main() -> int:
    """Probe the live API to verify property name corrections."""
    parser = argparse.ArgumentParser(
        description="Verify property name corrections against the live F5 XC API",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Mark verified corrections in the config file",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help="Property name corrections config file",
    )
    args = parser.parse_args()

    if not args.config.exists():
        console.print(f"[red]Config not found: {args.config}[/red]")
        return 1

    with args.config.open() as fh:
        config = yaml.safe_load(fh) or {}
    corrections = config.get("corrections", [])

    if not corrections:
        console.print("[yellow]No corrections to verify[/yellow]")
        return 0

    console.print("[bold blue]Verifying property names against live API[/bold blue]")

    with VALIDATION_CONFIG_PATH.open() as fh:
        val_config = yaml.safe_load(fh) or {}

    try:
        auth = load_auth_from_config(val_config)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        console.print(
            "[yellow]Set F5XC_API_URL and F5XC_API_TOKEN to probe the API[/yellow]"
        )
        return 1

    results = []
    with auth:
        for correction in corrections:
            console.print(
                f"  Probing {correction['schema']}.{correction['old_key']}..."
            )
            result = _probe_correction(auth, correction)
            results.append(result)

    table = Table(title="Property Name Verification Results")
    table.add_column("Schema", style="cyan")
    table.add_column("Old Key", style="red")
    table.add_column("New Key", style="green")
    table.add_column("Status", style="bold")
    table.add_column("Recommendation")

    status_styles = {
        "fix_spec": "green",
        "upstream_typo": "yellow",
        "both_present": "magenta",
        "neither_found": "dim",
        "error": "red",
    }

    for result in results:
        style = status_styles.get(result["status"], "white")
        table.add_row(
            result["schema"],
            result["old_key"],
            result["new_key"],
            f"[{style}]{result['status']}[/{style}]",
            result["recommendation"],
        )

    console.print(table)

    if args.apply:
        changed = _update_config(corrections, results)
        if changed:
            with args.config.open("w") as fh:
                yaml.dump(config, fh, default_flow_style=False, sort_keys=False)
            console.print("[green]Config updated — verified corrections marked[/green]")
        else:
            console.print("[yellow]No new corrections to verify[/yellow]")

    fix_count = sum(1 for r in results if r["status"] == "fix_spec")
    upstream_count = sum(1 for r in results if r["status"] == "upstream_typo")
    console.print(
        f"\n  Fix in spec: {fix_count}  |  "
        f"Upstream typo: {upstream_count}  |  "
        f"Other: {len(results) - fix_count - upstream_count}"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
