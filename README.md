# F5 XC API Spec Validation Framework

Reconcile live F5 Distributed Cloud API behavior with published OpenAPI specs, producing validated/fixed spec files released via GitHub releases.

## Overview

This framework validates F5 XC OpenAPI specifications against the live API, identifies
discrepancies, and produces corrected spec files. The release package contains either:

- **Fixed specs** where modifications were needed
- **Original specs** where no modifications were needed (pass-through)

## Quick Start

```bash
# Install dependencies
make install

# Run full pipeline
make all
```

## Requirements

- Python 3.10+
- F5 XC API Token with appropriate permissions
- Network access to F5 XC console

## Configuration

Set environment variables:

```bash
export F5XC_API_URL=https://f5-amer-ent.console.ves.volterra.io
export F5XC_API_TOKEN=<your-api-token>
```

Or create a `.env` file:

```env
F5XC_API_URL=https://f5-amer-ent.console.ves.volterra.io
F5XC_API_TOKEN=your-api-token-here
```

## Usage

### Individual Commands

```bash
# Download OpenAPI specs from F5
make download

# Validate against live API
make validate

# Dry run (no live API calls)
make validate-dry

# Run Schemathesis property-based tests
make schemathesis

# Generate reconciled specs
make reconcile

# Build release package
make release
```

### Full Pipeline

```bash
make all  # download → validate → reconcile → release
```

## Target Endpoints (Baseline)

| Resource | API Endpoint | Domain File |
|----------|-------------|-------------|
| healthcheck | healthchecks | virtual.json |
| origin_pool | origin_pools | virtual.json |
| app_firewall | app_firewalls | virtual.json |
| service_policy | service_policys | virtual.json |
| api_definition | api_definitions | api.json |
| api_discovery | api_discoverys | api.json |
| api_groups | api_groups | api.json |
| code_base_integration | code_base_integrations | api.json |
| data_type | data_types | data_and_privacy_security.json |
| sensitive_data_policy | sensitive_data_policys | data_and_privacy_security.json |

## Validation Categories

1. **String length** - minLength, maxLength boundaries
2. **Pattern/regex** - pattern validation
3. **Numeric bounds** - minimum, maximum ranges
4. **Required fields** - required array validation
5. **Enum values** - enumeration constraints
6. **Array bounds** - minItems, maxItems, uniqueItems
7. **Object structure** - additionalProperties, properties
8. **Composition** - oneOf, anyOf, allOf
9. **Dependencies** - dependentRequired
10. **Data types** - type, format validation

## Project Structure

```
f5xc-api-fixed/
├── .github/workflows/
│   └── validate-and-release.yml    # CI/CD pipeline
├── config/
│   ├── validation.yaml             # Main configuration
│   └── endpoints.yaml              # Target endpoint definitions
├── scripts/
│   ├── download.py                 # Spec download with ETag caching
│   ├── validate.py                 # Validation orchestrator
│   ├── reconcile.py                # Spec reconciliation engine
│   ├── release.py                  # Release package generator
│   └── utils/
│       ├── spec_loader.py          # OpenAPI spec loading
│       ├── constraint_validator.py # Constraint validation logic
│       ├── schemathesis_runner.py  # Schemathesis integration
│       ├── report_generator.py     # Report generation
│       └── auth.py                 # Authentication handling
├── tests/                          # Unit/integration tests
├── specs/original/                 # Downloaded specs (gitignored)
├── release/                        # Release package (gitignored)
└── reports/                        # Generated reports (gitignored)
```

## Release Package Contents

```
f5xc-api-fixed-vX.Y.Z.zip
├── openapi.json              # Fixed or original
├── openapi.yaml              # Fixed or original
├── domains/
│   ├── virtual.json          # Fixed or original
│   ├── api.json              # Fixed or original
│   └── ...                   # All domain files
├── CHANGELOG.md              # List of fixes applied
└── VALIDATION_REPORT.md      # Summary of validation results
```

## Development

```bash
# Install dev dependencies
make dev-install

# Run tests
make test

# Run linter
make lint

# Run type checker
make typecheck
```

## CI/CD

The GitHub Actions workflow runs daily at 6 AM UTC:

1. Downloads latest specs (with ETag check)
2. Runs validation against live API
3. Reconciles specs (fixed + pass-through originals)
4. Creates GitHub release with complete spec package
5. Uploads validation reports as artifacts

## License

MIT
