#!/usr/bin/env bash
# Translate staged English documentation with Antigravity in an isolated snapshot.
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "[i18n] must run inside a git repository" >&2
  exit 1
}
cd "$repo_root"

locales=(fr es de pt-br ja ko zh-cn zh-tw ar it hi th)
work=$(mktemp -d "${TMPDIR:-/tmp}/antigravity-translate.XXXXXX")
trap 'rm -rf "$work"' EXIT

snapshot="$work/snapshot"
before="$work/before"
active="$work/active"
deleted="$work/deleted"
allowed="$work/allowed"
requests="$work/requests"
mkdir -p "$snapshot" "$before"
: >"$active"
: >"$deleted"
: >"$allowed"
: >"$requests"

is_english_doc() {
  case "$1" in
  docs/en/*.md | docs/en/*.mdx | src/content/docs/en/*.md | src/content/docs/en/*.mdx) return 0 ;;
  *) return 1 ;;
  esac
}

target_path() {
  local source="$1" locale="$2" relative
  case "$source" in
  docs/en/*)
    relative=${source#docs/en/}
    printf 'docs/%s/%s' "$locale" "$relative"
    ;;
  src/content/docs/en/*)
    relative=${source#src/content/docs/en/}
    printf 'src/content/docs/%s/%s' "$locale" "$relative"
    ;;
  *) return 1 ;;
  esac
}

record_path() {
  local destination="$1" path="$2"
  case "$path" in
  *$'\n'* | *$'\t'*)
    echo "[i18n] documentation paths containing tabs or newlines are unsupported: $path" >&2
    exit 1
    ;;
  esac
  printf '%s\n' "$path" >>"$destination"
}

while IFS= read -r -d '' path; do
  is_english_doc "$path" && record_path "$active" "$path"
done < <(git diff --cached --no-renames --name-only --diff-filter=ACM -z -- \
  docs/en src/content/docs/en)

while IFS= read -r -d '' path; do
  is_english_doc "$path" && record_path "$deleted" "$path"
done < <(git diff --cached --no-renames --name-only --diff-filter=D -z -- \
  docs/en src/content/docs/en)

if [ ! -s "$active" ] && [ ! -s "$deleted" ]; then
  echo "[i18n] no staged English documentation changes"
  exit 0
fi

git checkout-index --all --force --prefix="${snapshot}/"
cp -R "${snapshot}/." "$before/"

while IFS= read -r source; do
  [ -n "$source" ] || continue
  source_file="$snapshot/$source"
  if [ ! -f "$source_file" ]; then
    echo "[i18n] staged English source is missing from the index snapshot: $source" >&2
    exit 1
  fi
  source_hash=$(shasum -a 256 "$source_file" | awk '{print substr($1, 1, 12)}')
  needs_translation=false
  for locale in "${locales[@]}"; do
    target=$(target_path "$source" "$locale")
    target_file="$snapshot/$target"
    stored_hash=""
    if [ -f "$target_file" ]; then
      stored_hash=$(grep -m1 -E '^[[:space:]]*sourceHash:' "$target_file" |
        grep -Eo '[0-9a-f]{12}' | head -1 || true)
    fi
    if [ "$stored_hash" != "$source_hash" ]; then
      needs_translation=true
    fi
  done
  if [ "$needs_translation" = true ]; then
    printf 'translate\t%s\t%s\n' "$source" "$source_hash" >>"$requests"
    for locale in "${locales[@]}"; do
      record_path "$allowed" "$(target_path "$source" "$locale")"
    done
  fi
done <"$active"

while IFS= read -r source; do
  [ -n "$source" ] || continue
  printf 'delete\t%s\t-\n' "$source" >>"$requests"
  for locale in "${locales[@]}"; do
    target=$(target_path "$source" "$locale")
    record_path "$allowed" "$target"
    rm -f "$snapshot/$target"
  done
done <"$deleted"

sort -u "$allowed" -o "$allowed"

if grep -q '^translate[[:space:]]' "$requests"; then
  if ! command -v agy >/dev/null 2>&1; then
    echo "[i18n] Antigravity translation requires agy in the developer environment" >&2
    exit 1
  fi

  prompt_file="$work/prompt"
  {
    echo "Read .agents/skills/i18n-translate/SKILL.md and translate only the staged-index snapshot described below."
    echo "The snapshot is not a git repository. Do not run git, commit, stage, push, use GitHub, or change any file except the locale targets for these sources."
    echo "Preserve code, inline code, imports, exports, MDX/JSX tags and attributes, URLs, file paths, and product names exactly."
    echo "Every target must set i18n.sourceHash to the supplied hash and i18n.translator to machine."
    echo "Translate every source into exactly these locales: fr es de pt-br ja ko zh-cn zh-tw ar it hi th."
    echo "Requests:"
    awk -F '\t' '$1 == "translate" { printf "- %s sourceHash=%s\n", $2, $3 }' "$requests"
  } >"$prompt_file"

  if ! (
    cd "$snapshot"
    env -u GH_TOKEN -u GITHUB_TOKEN -u REPO_SETTINGS_TOKEN -u REPO_SYNC_TOKEN \
      agy --new-project --sandbox --mode accept-edits --disable-slash-commands \
      --print-timeout 25m --print "$(cat "$prompt_file")"
  ); then
    echo "[i18n] Antigravity translation failed; the commit remains blocked" >&2
    exit 1
  fi
fi

python3 - "$before" "$snapshot" "$allowed" "$requests" <<'PY'
from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path
import re
import stat
import sys

before = Path(sys.argv[1])
after = Path(sys.argv[2])
allowed = {line for line in Path(sys.argv[3]).read_text().splitlines() if line}
requests = []
for line in Path(sys.argv[4]).read_text().splitlines():
    if line:
        requests.append(tuple(line.split("\t")))

locales = ("fr", "es", "de", "pt-br", "ja", "ko", "zh-cn", "zh-tw", "ar", "it", "hi", "th")

def entries(root: Path) -> dict[str, tuple[str, int, bytes | str]]:
    result = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            result[rel] = ("symlink", stat.S_IMODE(mode), path.readlink().as_posix())
        elif path.is_file():
            result[rel] = ("file", stat.S_IMODE(mode), path.read_bytes())
    return result

old = entries(before)
new = entries(after)
changed = {path for path in old.keys() | new.keys() if old.get(path) != new.get(path)}
unexpected = sorted(changed - allowed)
if unexpected:
    raise SystemExit("Antigravity changed paths outside the locale allowlist: " + ", ".join(unexpected))

def target_path(source: str, locale: str) -> str:
    if source.startswith("docs/en/"):
        return f"docs/{locale}/{source.removeprefix('docs/en/')}"
    if source.startswith("src/content/docs/en/"):
        return f"src/content/docs/{locale}/{source.removeprefix('src/content/docs/en/')}"
    raise SystemExit(f"unsupported English source path: {source}")

def protected_tokens(raw: str) -> Counter[str]:
    patterns = (
        r"(?ms)^(?:```|~~~).*?^(?:```|~~~)[ \t]*$",
        r"(?<!`)`[^`\n]+`(?!`)",
        r"(?m)^(?:import|export)\b.*$",
        r"<[A-Za-z][^>\n]*>",
        r"https?://[^\s)>]+",
        r"\]\(([^)]+)\)",
    )
    tokens: Counter[str] = Counter()
    for pattern in patterns:
        for match in re.finditer(pattern, raw):
            tokens[match.group(0)] += 1
    return tokens

for action, source, expected_hash in requests:
    targets = [target_path(source, locale) for locale in locales]
    if action == "delete":
        remaining = [target for target in targets if (after / target).exists()]
        if remaining:
            raise SystemExit("deleted English source retained locale targets: " + ", ".join(remaining))
        continue
    if action != "translate":
        raise SystemExit(f"unknown translation request action: {action}")

    source_file = after / source
    actual_hash = hashlib.sha256(source_file.read_bytes()).hexdigest()[:12]
    if actual_hash != expected_hash:
        raise SystemExit(f"staged source changed while translating {source}")
    source_tokens = protected_tokens(source_file.read_text(encoding="utf-8"))

    for target in targets:
        target_file = after / target
        if not target_file.is_file() or target_file.is_symlink():
            raise SystemExit(f"missing regular translation file: {target}")
        raw = target_file.read_text(encoding="utf-8")
        hash_match = re.search(r'^\s*sourceHash:\s*["\']?([0-9a-f]{12})["\']?\s*$', raw, re.MULTILINE)
        translator_match = re.search(r'^\s*translator:\s*["\']?machine["\']?\s*$', raw, re.MULTILINE)
        if not hash_match or hash_match.group(1) != expected_hash:
            raise SystemExit(f"wrong or missing i18n.sourceHash in {target}")
        if not translator_match:
            raise SystemExit(f"wrong or missing i18n.translator in {target}")
        if protected_tokens(raw) != source_tokens:
            raise SystemExit(f"protected MDX/code/URL tokens changed in {target}")
PY

while IFS= read -r target; do
  [ -n "$target" ] || continue
  if [ -f "$snapshot/$target" ]; then
    mkdir -p "$(dirname "$target")"
    cp -p "$snapshot/$target" "$target"
    git add -- "$target"
  else
    git rm -f --ignore-unmatch -- "$target" >/dev/null
  fi
done <"$allowed"

echo "[i18n] Antigravity translations validated and staged"
