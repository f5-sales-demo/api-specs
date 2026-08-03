---
name: i18n-translate
description: Translate English documentation under docs/en/ or src/content/docs/en/ into target locales across the f5-sales-demo ecosystem using Antigravity (agy). Preserves MDX components, code blocks, frontmatter schemas, and updates i18n.sourceHash.
---

# Antigravity (`agy`) Ecosystem Document Translation

This skill guides `agy` in translating English Markdown and MDX documentation (`docs/en/**/*.md[x]` or `src/content/docs/en/**/*.md[x]`) into the 12 ecosystem target locales.

## Target Locales Registry

| Locale Code | Language Name         |
| ----------- | --------------------- |
| `fr`        | French                |
| `es`        | Spanish               |
| `de`        | German                |
| `pt-br`     | Portuguese (Brazil)   |
| `ja`        | Japanese              |
| `ko`        | Korean                |
| `zh-cn`     | Chinese (Simplified)  |
| `zh-tw`     | Chinese (Traditional) |
| `ar`        | Arabic                |
| `it`        | Italian               |
| `hi`        | Hindi                 |
| `th`        | Thai                  |

---

## Translation Procedure

### 1. Compute English Source Hash

For every English document (`docs/en/path/to/file.md[x]`), calculate the **SHA-256 hash** of the raw English file contents and take the first **12 hexadecimal characters**:

```python
import hashlib
source_hash = hashlib.sha256(english_raw_bytes).hexdigest()[:12]
```

### 2. Identify Stale or Missing Target Translations

Check target files at `docs/<locale>/path/to/file.md[x]`:

- If the target file does **not exist**, it is **MISSING** and requires translation.
- If the target file exists, check its YAML frontmatter `i18n.sourceHash`:
  - If `i18n.sourceHash != source_hash`, it is **STALE** and requires translation.
  - If `i18n.sourceHash == source_hash`, it is up to date and can be skipped (unless `--force` is specified).

### 3. Handle Deleted English Documentation

Check for English source files that have been **deleted** from `docs/en/` or `src/content/docs/en/`:

- Locate their corresponding target translation paths:
  - If `docs/en/path/to/file.md[x]` is deleted -> Delete `docs/<locale>/path/to/file.md[x]` for all 12 target locales.
  - If `src/content/docs/en/path/to/file.md[x]` is deleted -> Delete `src/content/docs/<locale>/path/to/file.md[x]` for all 12 target locales.
- Stage these deletions:

  ```bash
  git rm docs/<locale>/path/to/file.md[x]
  ```

---

## Content & Formatting Guidelines

### Frontmatter Schema Rules

- Preserve all structural YAML frontmatter keys from the English source.
- **Translate** human-readable text fields:
  - `title`
  - `description`
  - `sidebar.label`
  - `hero.title`
  - `hero.tagline`
  - `hero.actions[].text`
- **Update or inject** the `i18n` metadata block:

  ```yaml
  i18n:
    sourceHash: "<12-character-sha256-hex>"
    translator: "machine"
  ```

### Non-Translatable Elements

1. **Code Blocks & Inline Code**: Keep code blocks (fenced with ``` or ~~~) and inline code (`...`) completely untranslated.
2. **JSX / MDX Elements**: Keep HTML/JSX component tags intact, including attributes, imports (`import ... from '...'`), and exports (`export ...`).
3. **Brand & Product Names**: Keep technical product names untranslated (e.g., `F5`, `F5 Distributed Cloud`, `XC`, `NGINX`, `Terraform`, `Kubernetes`, `Docker`).
4. **URLs & File Paths**: Do not translate image paths, hyperlinks, or absolute/relative URLs unless locale-specific pathing is explicitly required.

---

## Workspace Execution via `agy`

When running on GitHub Action runners or in CLI:

1. Identify all changed English documentation files using `git diff` or filesystem scanning.
2. For each stale/missing locale, generate the localized translation.
3. Write the result to `docs/<locale>/path/to/file.md[x]`.
4. Stage and commit the updated target files:

   ```bash
   git add docs/<locale>/path/to/file.md[x]
   ```
