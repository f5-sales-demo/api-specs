.PHONY: help install dev-install docs-install download validate reconcile release test lint typecheck clean all docs docs-serve docs-generate pre-commit pre-commit-install pre-commit-update spectral-lint spectral-gate identifier-gate transform regenerate-specs spell-check-specs verify-property-names

PYTHON := python3
VENV := .venv
BIN := $(VENV)/bin

help:
	@echo "F5 XC API Spec Validation Framework"
	@echo ""
	@echo "Usage:"
	@echo "  make install       Install production dependencies"
	@echo "  make dev-install   Install development dependencies"
	@echo "  make docs-install  Install documentation dependencies"
	@echo "  make download      Download OpenAPI specs from F5"
	@echo "  make validate      Run validation against live API"
	@echo "  make reconcile     Generate reconciled specs"
	@echo "  make release       Build release package"
	@echo "  make test          Run unit tests"
	@echo "  make lint          Run linter"
	@echo "  make typecheck     Run type checker"
	@echo "  make spell-check-specs  Check spelling in spec text fields and property names"
	@echo "  make verify-property-names  Verify property names against live API"
	@echo "  make clean         Clean generated files"
	@echo "  make all           Full pipeline: download → validate → reconcile → release"
	@echo "  make spectral-lint Run Spectral OAS3 linting (pre-reconcile)"
	@echo "  make spectral-gate Run Spectral quality gate (post-reconcile)"
	@echo ""
	@echo "Pre-commit:"
	@echo "  make pre-commit-install  Install pre-commit hooks"
	@echo "  make pre-commit          Run all pre-commit hooks"
	@echo "  make pre-commit-update   Update pre-commit hooks"
	@echo ""
	@echo "Documentation:"
	@echo "  make docs          Build MkDocs documentation"
	@echo "  make docs-serve    Serve docs locally for preview"
	@echo "  make docs-generate Generate docs from validation reports"

$(VENV)/bin/activate:
	$(PYTHON) -m venv $(VENV)

install: $(VENV)/bin/activate
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e .

dev-install: $(VENV)/bin/activate
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

docs-install: $(VENV)/bin/activate
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[docs]"

download:
	$(BIN)/python -m scripts.download

validate:
	$(BIN)/python -m scripts.validate

reconcile:
	$(BIN)/python -m scripts.reconcile --report reports/validation_report.json

spectral-lint:
	$(BIN)/python -m scripts.spectral_lint --mode discover

spectral-gate:
	$(BIN)/python -m scripts.spectral_lint --mode gate

identifier-gate:
	$(BIN)/python -m scripts.example_identifier_safety --spec-dir release/specs

transform:
	$(BIN)/python -m scripts.transform

# Bring the committed release/specs artifact back in step with config/.
#
# A correction only has value once it reaches the published artifact, so a PR that changes
# config/ must also commit the regenerated specs -- tests/test_release_specs_current.py
# enforces that. This is the one command that satisfies it.
#
# Reads and writes release/specs in place: transform_all() loads every spec before
# save_results() writes any, so the round trip is safe. Provenance comes from
# release/specs/.spec_metadata.json, so info.version reflects the drop the artifact was built
# from rather than whatever was last downloaded locally. Requires no network and no
# F5XC_API_TOKEN, and is idempotent -- a second run produces no diff.
regenerate-specs:
	$(BIN)/python -m scripts.transform --input-dir release/specs --output-dir release/specs

spell-check-specs:
	$(BIN)/python -m scripts.spell_check_specs

verify-property-names:
	$(BIN)/python -m scripts.verify_property_names

release:
	@: "$${VERSION:?set VERSION to the exact release version}"
	@: "$${BUILD_TIMESTAMP:?set BUILD_TIMESTAMP to specs/original/.spec_metadata.json spec_timestamp}"
	$(BIN)/python -m scripts.release --version "$$VERSION" --build-timestamp "$$BUILD_TIMESTAMP"

test:
	$(BIN)/pytest tests/ -v --cov=scripts --cov-report=term-missing

lint:
	$(BIN)/ruff check scripts/ tests/
	$(BIN)/ruff format --check scripts/ tests/

format:
	$(BIN)/ruff format scripts/ tests/
	$(BIN)/ruff check --fix scripts/ tests/

typecheck:
	$(BIN)/mypy --config-file .mypy.ini scripts/ tests/

clean:
	rm -rf specs/original/*
	rm -rf reports/*
	rm -rf release/*.zip
	rm -rf site/
	rm -rf .pytest_cache
	rm -rf .coverage
	rm -rf htmlcov
	rm -rf __pycache__
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

all: download transform spectral-lint validate reconcile spectral-gate release
	@echo "Full pipeline completed"

# CI/CD targets
ci-test: dev-install test lint typecheck

ci-validate: install download transform spectral-lint validate reconcile spectral-gate release

# Documentation targets
docs-generate:
	$(BIN)/python scripts/generate_docs.py --reconciliation-report reports/reconciliation_report.json

docs: docs-generate
	$(BIN)/mkdocs build --strict

docs-serve: docs-generate
	$(BIN)/mkdocs serve

# Pre-commit targets
pre-commit-install: dev-install
	$(BIN)/pre-commit install --install-hooks
	$(BIN)/pre-commit install --hook-type commit-msg

pre-commit: dev-install
	$(BIN)/pre-commit run --all-files

pre-commit-update:
	$(BIN)/pre-commit autoupdate
