#!/usr/bin/env bash
# Repository-specific pre-commit hooks for api-specs
# Called by the universal .pre-commit-config.yaml local-hooks entry
set -euo pipefail

resolve_tool() {
  local tool=$1
  if [[ -x ".venv/bin/${tool}" ]]; then
    printf '%s\n' ".venv/bin/${tool}"
    return
  fi
  if command -v "$tool" >/dev/null 2>&1; then
    command -v "$tool"
    return
  fi
  printf '[local] required tool is unavailable: %s\n' "$tool" >&2
  return 1
}

staged_files=()
while IFS= read -r -d '' file; do
  staged_files+=("$file")
done < <(git diff --cached --name-only --diff-filter=ACM -z)

python_files=()
for file in "${staged_files[@]}"; do
  if [[ "$file" == *.py ]]; then
    python_files+=("$file")
  fi
done

if [[ ${#python_files[@]} -gt 0 ]]; then
  ruff=$(resolve_tool ruff)
  mypy=$(resolve_tool mypy)

  echo "[local] Linting staged Python files with Ruff..."
  "$ruff" check "${python_files[@]}"
  "$ruff" format --check "${python_files[@]}"

  echo "[local] Type checking staged Python files with mypy..."
  "$mypy" --config-file .mypy.ini "${python_files[@]}"
fi

echo "[local] All repo-specific checks passed."
