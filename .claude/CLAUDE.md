# API Specs -- Repo-Specific Instructions

## Project Overview
Python-based OpenAPI spec validation and reconciliation framework for F5 Distributed Cloud.
Downloads official specs, validates them against the live API, reconciles discrepancies, and releases corrected specs.

## Key Commands
- `make install` -- production setup
- `make dev-install` -- dev setup with testing tools
- `make validate` -- run full spec validation
- `make reconcile` -- reconcile specs with discovered API behavior
- `make release` -- build release package
- `make test` -- run pytest suite
- `make lint` -- run ruff linter
- `make all` -- full pipeline: download -> validate -> reconcile -> release

## Directory Structure
- `scripts/` -- Python pipeline scripts (download, validate, reconcile, release)
- `scripts/utils/` -- Shared utilities (auth, constraint_validator, report_generator, etc.)
- `config/` -- Pipeline configuration (endpoints.yaml, validation.yaml)
- `release/specs/` -- Released OpenAPI spec files (268 specs)
- `tests/` -- Test suite
- `docs/` -- MDX documentation (Starlight format)

## Environment Variables
```
F5XC_API_URL=https://f5-amer-ent.console.ves.volterra.io
F5XC_API_TOKEN=<your-api-token>
```

## CI Pipeline
- `validate-and-release.yml` -- daily spec validation and release (6 AM UTC)
- Governance workflows managed by docs-control
