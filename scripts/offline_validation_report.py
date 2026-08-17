"""Write the deterministic validation receipt used by offline release CI."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


def count_operations(spec_dir: Path) -> int:
    """Count OpenAPI operations from transformed JSON documents without network access."""
    count = 0
    for path in sorted(spec_dir.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        paths = document.get("paths", {})
        if not isinstance(paths, dict):
            raise ValueError(f"{path} paths must be an object")
        for operations in paths.values():
            if not isinstance(operations, dict):
                continue
            count += sum(
                1
                for method, operation in operations.items()
                if method.lower() in {"get", "put", "post", "delete", "patch", "head", "options"}
                and isinstance(operation, dict)
            )
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write an offline validation report")
    parser.add_argument("--spec-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    endpoints = count_operations(args.spec_dir)
    report = {
        "summary": {
            "timestamp": datetime.now(UTC).isoformat(),
            "mode": "offline",
            "total_endpoints": endpoints,
            "total_tests": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "total_discrepancies": 0,
            "discrepancies_by_type": {},
            "modified_files": [],
            "unmodified_files": [],
        },
        "results": [],
        "discrepancies": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
