from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from .strict_data import StrictDataError, strict_json_loads


class ReconciliationReportError(ValueError):
    """Exception raised for any reconciliation report validation or operation errors."""

    pass


# Resolve the schema file path relative to this module
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "config" / "reconciliation_report.schema.json"


def _load_schema() -> dict[str, Any]:
    """Load and verify the JSON Schema of the reconciliation report."""
    if not SCHEMA_PATH.exists():
        raise ReconciliationReportError(f"Reconciliation report schema not found at {SCHEMA_PATH}")

    try:
        content = SCHEMA_PATH.read_text(encoding="utf-8")
        # Use strict loads to ensure the schema itself is perfectly valid and has no duplicate keys
        schema = strict_json_loads(content, str(SCHEMA_PATH))
        # Validate that the schema itself is structured as a valid Draft-07 schema
        Draft7Validator.check_schema(schema)
        return schema
    except Exception as error:
        raise ReconciliationReportError(f"Failed to load or verify JSON Schema: {error}") from error


# Load the schema eagerly so any schema-definition errors fail fast during import
SCHEMA = _load_schema()


def validate_reconciliation_report(data: dict[str, Any]) -> None:
    """Validate a reconciliation report dictionary against JSON Schema and semantic invariants."""
    # 1. Structural validation via JSON Schema with a Draft7Validator and FormatChecker
    validator = Draft7Validator(SCHEMA, format_checker=FormatChecker())
    try:
        validator.validate(data)
    except ValidationError as error:
        raise ReconciliationReportError(
            f"Schema validation failed: {error.message} at path '{error.json_path}'"
        ) from error

    # 2. Extract sections for semantic invariant checks
    summary = data["summary"]
    fixes = data.get("fixes", [])
    failures = data.get("failures", [])

    processed_specs = summary["processed_specs"]
    discrepancies_received = summary["discrepancies_received"]
    fixes_applied = summary["fixes_applied"]
    sum_failures = summary["failures"]
    modified_files = summary["modified_files"]
    unmodified_files = summary["unmodified_files"]

    # 3. Invariant: summary.fixes_applied == len(fixes)
    if fixes_applied != len(fixes):
        raise ReconciliationReportError(
            f"Semantic invariant violation: summary.fixes_applied ({fixes_applied}) must equal length of fixes list ({len(fixes)})"
        )

    # 4. Invariant: summary.failures == len(failures)
    if sum_failures != len(failures):
        raise ReconciliationReportError(
            f"Semantic invariant violation: summary.failures ({sum_failures}) must equal length of failures list ({len(failures)})"
        )

    # 5. Invariant: summary.discrepancies_received == len(fixes) + len(failures)
    expected_received = len(fixes) + len(failures)
    if discrepancies_received != expected_received:
        raise ReconciliationReportError(
            f"Semantic invariant violation: summary.discrepancies_received ({discrepancies_received}) must equal total outcomes ({expected_received})"
        )

    # 6. Invariant: Each outcome's top-level spec_file equals its source record's spec_file
    for i, fix in enumerate(fixes):
        top_spec = fix["spec_file"]
        source_spec = fix["source_discrepancy"].get("spec_file")
        if top_spec != source_spec:
            raise ReconciliationReportError(
                f"Semantic invariant violation: Fix #{i} has top-level spec_file '{top_spec}' but source record spec_file '{source_spec}'"
            )

    for i, fail in enumerate(failures):
        top_spec = fail["spec_file"]
        source_spec = fail["source_discrepancy"].get("spec_file")
        if top_spec != source_spec:
            raise ReconciliationReportError(
                f"Semantic invariant violation: Failure #{i} has top-level spec_file '{top_spec}' but source record spec_file '{source_spec}'"
            )

    # 7. Invariant: Modified and unmodified file lists are unique and disjoint
    modified_set = set(modified_files)
    unmodified_set = set(unmodified_files)

    if len(modified_files) != len(modified_set):
        raise ReconciliationReportError(
            "Semantic invariant violation: Modified files contains duplicate entries"
        )
    if len(unmodified_files) != len(unmodified_set):
        raise ReconciliationReportError(
            "Semantic invariant violation: Unmodified files contains duplicate entries"
        )

    intersection = modified_set & unmodified_set
    if intersection:
        raise ReconciliationReportError(
            f"Semantic invariant violation: Modified and unmodified file lists must be disjoint. Overlap: {intersection}"
        )

    # 8. Invariant: Their union contains every processed spec exactly once
    union_set = modified_set | unmodified_set
    if len(union_set) != processed_specs:
        raise ReconciliationReportError(
            f"Semantic invariant violation: Processed specs count ({processed_specs}) must equal size of file union ({len(union_set)})"
        )

    # 9. Invariant: Every fix references a modified file
    for i, fix in enumerate(fixes):
        spec = fix["spec_file"]
        if spec not in modified_set:
            raise ReconciliationReportError(
                f"Semantic invariant violation: Fix #{i} references file '{spec}' which is not in the modified files list"
            )

    # 10. Invariant: Unmodified files have no fix entries
    for i, fix in enumerate(fixes):
        spec = fix["spec_file"]
        if spec in unmodified_set:
            raise ReconciliationReportError(
                f"Semantic invariant violation: Fix #{i} references unmodified file '{spec}'"
            )

    # Validate timestamp format (using schema's format validation, which FormatChecker covers)
    # Draft7Validator with FormatChecker validates date-time, but let's double check it or ensure FormatChecker was effective
    pass


def load_reconciliation_report(file_path: str | Path) -> dict[str, Any]:
    """Load, strictly parse, and validate a reconciliation report from a JSON file."""
    path = Path(file_path)
    if not path.exists():
        raise ReconciliationReportError(f"Reconciliation report file does not exist: {file_path}")

    try:
        content = path.read_text(encoding="utf-8")
        # Use strict_json_loads to reject duplicates and non-finite values (like NaN/Infinity)
        data = strict_json_loads(content, str(path))
    except StrictDataError as error:
        raise ReconciliationReportError(f"Strict parse validation failed: {error}") from error
    except Exception as error:
        raise ReconciliationReportError(f"Failed to read reconciliation report: {error}") from error

    validate_reconciliation_report(data)
    return data


def write_reconciliation_report(data: dict[str, Any], file_path: str | Path) -> None:
    """Validate and atomically write a reconciliation report to a JSON file."""
    path = Path(file_path).resolve()

    # Run full structural and semantic validation first
    validate_reconciliation_report(data)

    # Ensure the parent directory exists
    path.parent.mkdir(parents=True, exist_ok=True)

    # Serialize to standard indented JSON format safely, rejecting NaN/Infinities recursively
    try:
        serialized = json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n"
        serialized_bytes = serialized.encode("utf-8")
    except ValueError as error:
        raise ReconciliationReportError(f"JSON value must be finite: {error}") from error
    except Exception as error:
        raise ReconciliationReportError(
            f"Failed to serialize reconciliation report: {error}"
        ) from error

    # Perform atomic write via same-directory temporary file replacement
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-recon-")
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(serialized_bytes)
        os.replace(tmp_path, str(path))
    except Exception as error:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise ReconciliationReportError(
            f"Failed to atomically write reconciliation report: {error}"
        ) from error
