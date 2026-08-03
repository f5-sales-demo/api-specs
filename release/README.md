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
make release

# Or with specific version
python -m scripts.release --version 1.0.0
```

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
