# F5 XC API Fixed Specs

Validated and reconciled F5 Distributed Cloud OpenAPI specifications.

This project validates F5 XC OpenAPI specifications against the live API, identifies discrepancies, and produces corrected spec files.

[View All Modifications](modifications/index.md){ .md-button .md-button--primary }

## Downloads

Get the latest validated specs from the [GitHub Releases](https://github.com/robinmordasiewicz/f5xc-api-fixed/releases) page.

## Validation Process

Each release contains specs that have been validated using:

- **OpenAPI Spec Validator** - Structural validation
- **Schemathesis** - Property-based testing against live API
- **Custom Constraint Validation** - Boundary testing for all constraint types

## Release Contents

- `openapi.json` / `openapi.yaml` - Merged OpenAPI specification
- `domains/` - Individual domain-specific spec files
- `CHANGELOG.md` - List of modifications applied
- `VALIDATION_REPORT.md` - Summary of validation results
