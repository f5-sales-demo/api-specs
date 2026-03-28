# Claude Code Project Instructions

## Managed Files

Files in `.claude/governance.json` are managed by docs-control.
A hook blocks direct edits — open an issue in docs-control instead.

## Git Operations

Delegate ALL Git/GitHub operations to `f5xc-github-ops:github-ops`.
Never run `git commit`, `git push`, `gh pr create` directly.

```
Agent(
  subagent_type="f5xc-github-ops:github-ops",
  prompt="<type>: <desc>\n\nFiles:\n- <list>\n\nWhy: <reason>"
)
```

See `CONTRIBUTING.md` for project rules.
