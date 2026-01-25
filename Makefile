.PHONY: help install dev-install docs-install download validate reconcile release test lint typecheck clean all docs docs-serve docs-generate

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
	@echo "  make validate-dry  Dry run validation (no live API calls)"
	@echo "  make schemathesis  Run Schemathesis property-based tests"
	@echo "  make reconcile     Generate reconciled specs"
	@echo "  make release       Build release package"
	@echo "  make test          Run unit tests"
	@echo "  make lint          Run linter"
	@echo "  make typecheck     Run type checker"
	@echo "  make clean         Clean generated files"
	@echo "  make all           Full pipeline: download → validate → reconcile → release"
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

validate-dry:
	$(BIN)/python -m scripts.validate --dry-run

schemathesis:
	$(BIN)/python -m scripts.validate --schemathesis-only

reconcile:
	$(BIN)/python -m scripts.reconcile

release:
	$(BIN)/python -m scripts.release

test:
	$(BIN)/pytest tests/ -v --cov=scripts --cov-report=term-missing

lint:
	$(BIN)/ruff check scripts/ tests/
	$(BIN)/ruff format --check scripts/ tests/

format:
	$(BIN)/ruff format scripts/ tests/
	$(BIN)/ruff check --fix scripts/ tests/

typecheck:
	$(BIN)/mypy scripts/

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

all: download validate reconcile release
	@echo "Full pipeline completed"

# CI/CD targets
ci-test: dev-install test lint typecheck

ci-validate: install download validate reconcile release

# Documentation targets
docs-generate:
	$(BIN)/python scripts/generate_docs.py

docs: docs-generate
	$(BIN)/mkdocs build --strict

docs-serve: docs-generate
	$(BIN)/mkdocs serve
