# Release Notes Template

This directory is used to build release packages.

## Contents

When a release is built, the following files will be included:

- `openapi.json` - Merged OpenAPI specification (JSON format)
- `openapi.yaml` - Merged OpenAPI specification (YAML format)
- `domains/` - Individual domain-specific spec files
- `CHANGELOG.md` - List of modifications applied to specs
- `VALIDATION_REPORT.md` - Summary of validation results
- `manifest.json` - File manifest with metadata

## Building a Release

```bash
# From project root
VERSION=2026.08.02-1 \
BUILD_TIMESTAMP=2026-07-30T15:32:53+00:00 \
make release

# Or directly
python -m scripts.release \
  --version 2026.08.02-1 \
  --build-timestamp 2026-07-30T15:32:53+00:00
```

The version and immutable build timestamp are mandatory. The timestamp must be
the `spec_timestamp` from `.spec_metadata.json`; it is the sole clock used for
the manifest, validation report, and ZIP members. A release also requires the
reconciled `CHANGELOG.md` and JSON validation report; missing evidence fails the
build.

## Release Strategy

Each release contains:

1. **Fixed specs** - Where discrepancies were found and corrected
2. **Original specs** - Where no modifications were needed (pass-through)

This ensures the release always contains a complete, valid set of OpenAPI specifications.

## Validation

All specs in a release have been:

1. Validated against OpenAPI Spec Validator
2. Tested with Schemathesis property-based testing
3. Verified against the live F5 XC API
4. Reconciled to match actual API behavior
