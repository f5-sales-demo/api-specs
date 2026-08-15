"""Sanitize and reject realistic identifier examples in published OpenAPI bytes.

Only deterministic, obviously synthetic UUIDs and RFC 5737 IPv4 addresses
belong in examples.  The checker never includes a matched value in its output.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

UUID_RE = re.compile(
    r"(?<![0-9A-Fa-f])"
    r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}"
    r"(?![0-9A-Fa-f])"
)
IPV4_WITH_CIDR_RE = re.compile(
    r"(?<![0-9.])"
    r"(?P<address>(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})"
    r"(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){3})"
    r"(?P<cidr>/(?:[0-9]|[12][0-9]|3[0-2]))?"
    r"(?![0-9.])"
)
SYNTHETIC_UUID_RE = re.compile(r"00000000-0000-4000-8000-[0-9a-f]{12}$")
DOCUMENTATION_NETWORKS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
)


@dataclass(frozen=True)
class IdentifierPolicy:
    """Explicit schema paths whose semantic constants are not examples."""

    schema_constant_paths: frozenset[str] = frozenset()


@dataclass(frozen=True, order=True)
class IdentifierFinding:
    """A redacted release-gate finding."""

    category: str
    path: str


def _pointer_segment(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _synthetic_uuid(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"00000000-0000-4000-8000-{digest}"


def _synthetic_ipv4(value: str) -> str:
    host = int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:2], 16) % 254 + 1
    return f"192.0.2.{host}"


def _sanitize_text(value: str) -> str:
    def replace_uuid(match: re.Match[str]) -> str:
        identifier = match.group(0)
        if SYNTHETIC_UUID_RE.fullmatch(identifier.lower()):
            return identifier
        return _synthetic_uuid(identifier)

    value = UUID_RE.sub(replace_uuid, value)

    def replace_ipv4(match: re.Match[str]) -> str:
        address = ipaddress.ip_address(match.group("address"))
        if _is_documentation_address(address) or not address.is_private:
            return match.group(0)
        # A documentation network is only RFC 5737 when represented as /24;
        # retain host examples but replace private network examples as a whole.
        if match.group("cidr"):
            return "192.0.2.0/24"
        return _synthetic_ipv4(match.group("address"))

    return IPV4_WITH_CIDR_RE.sub(replace_ipv4, value)


def sanitize_identifier_examples(spec: Any) -> Any:
    """Return a recursively sanitized copy of a JSON-compatible OpenAPI object."""
    if isinstance(spec, dict):
        return {key: sanitize_identifier_examples(value) for key, value in spec.items()}
    if isinstance(spec, list):
        return [sanitize_identifier_examples(value) for value in spec]
    return _sanitize_text(spec) if isinstance(spec, str) else spec


def _is_documentation_address(address: ipaddress.IPv4Address) -> bool:
    return any(address in network for network in DOCUMENTATION_NETWORKS)


def _walk_strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, f"{path}/{_pointer_segment(str(key))}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{path}/{index}")
    elif isinstance(value, str):
        yield path or "/", value


def find_unsafe_identifiers(spec: Any, policy: IdentifierPolicy) -> list[IdentifierFinding]:
    """Return redacted findings for realistic UUIDs and private IPv4 values."""
    findings: set[IdentifierFinding] = set()
    for path, value in _walk_strings(spec):
        if path in policy.schema_constant_paths:
            continue
        for match in UUID_RE.finditer(value):
            if not SYNTHETIC_UUID_RE.fullmatch(match.group(0).lower()):
                findings.add(IdentifierFinding("realistic-uuid", path))
        for match in IPV4_WITH_CIDR_RE.finditer(value):
            address = ipaddress.ip_address(match.group("address"))
            if address.is_private and not _is_documentation_address(address):
                findings.add(IdentifierFinding("private-ipv4", path))
    return sorted(findings)


def load_policy(path: Path) -> IdentifierPolicy:
    """Load explicit schema-constant exceptions from a reviewed policy file."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as error:
        raise ValueError(f"cannot read identifier policy: {path}") from error
    paths = raw.get("schema_constant_paths", [])
    if not isinstance(paths, list) or not all(
        isinstance(item, str) and item.startswith("/") for item in paths
    ):
        raise ValueError("identifier policy schema_constant_paths must be JSON-pointer strings")
    return IdentifierPolicy(schema_constant_paths=frozenset(paths))


def main(argv: list[str] | None = None) -> int:
    """Run the release gate without exposing matched identifier values."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec-dir", type=Path, default=Path("release/specs"))
    parser.add_argument(
        "--policy", type=Path, default=Path("config/example_identifier_policy.yaml")
    )
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        files = sorted(args.spec_dir.glob("*.json"))
        if not files:
            raise ValueError(f"no JSON specifications found in {args.spec_dir}")
        findings: list[tuple[str, IdentifierFinding]] = []
        for file in files:
            spec = json.loads(file.read_text(encoding="utf-8"))
            findings.extend(
                (file.name, finding) for finding in find_unsafe_identifiers(spec, policy)
            )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Identifier release gate error: {error}", file=sys.stderr)
        return 2
    if findings:
        for filename, finding in findings:
            print(
                f"::error file={filename}::[{finding.category}] realistic identifier ({finding.path})"
            )
        print(f"Identifier release gate: {len(findings)} finding(s).", file=sys.stderr)
        return 1
    print("Identifier release gate: clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
