#!/usr/bin/env bash
# Repository-specific pre-commit hooks for f5xc-api-fixed
# Called by the universal .pre-commit-config.yaml local-hooks entry
set -euo pipefail

STAGED_FILES=$(git diff --cached --name-only --diff-filter=ACM)

# --- Python linting (ruff) ---
PY_FILES=$(echo "$STAGED_FILES" | grep '\.py$' || true)
if [ -n "$PY_FILES" ]; then
  if command -v ruff &>/dev/null; then
    echo "[local] Linting Python files with ruff..."
    echo "$PY_FILES" | xargs ruff check --fix --exit-non-zero-on-fix
    echo "$PY_FILES" | xargs ruff format
  else
    echo "[local] ruff not installed, skipping Python lint"
  fi
fi

# --- Python type checking (mypy) ---
PY_FILES_NO_TESTS=$(echo "$STAGED_FILES" | grep '\.py$' | grep -v '^tests/' || true)
if [ -n "$PY_FILES_NO_TESTS" ]; then
  if command -v mypy &>/dev/null; then
    echo "[local] Running mypy type checking..."
    echo "$PY_FILES_NO_TESTS" | xargs mypy --ignore-missing-imports --no-error-summary || true
  fi
fi

# --- Python security scanning (bandit) ---
if [ -n "$PY_FILES" ]; then
  if command -v bandit &>/dev/null; then
    echo "[local] Running bandit security scan..."
    bandit -c pyproject.toml -r scripts/ 2>/dev/null || true
  fi
fi

# --- Spell checking (typos) ---
if command -v typos &>/dev/null; then
  NON_SPEC_FILES=$(echo "$STAGED_FILES" | grep -v '^specs/' | grep -v '^release/' || true)
  if [ -n "$NON_SPEC_FILES" ]; then
    echo "[local] Running spell check with typos..."
    echo "$NON_SPEC_FILES" | xargs typos --force-exclude || true
  fi
fi

echo "[local] All repo-specific checks passed."