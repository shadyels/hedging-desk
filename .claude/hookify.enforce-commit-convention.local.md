---
name: enforce-commit-convention
enabled: true
event: bash
action: block
pattern: git\s+commit\b[^\n]*?-m\s+(?!["\']?(?:feat|fix|chore|docs|refactor|build|ci|perf|style|test|revert)(?:\([^)]+\))?!?:\s)
---

Commit message doesn't follow Conventional Commits (`CLAUDE.md`).

Use `type(scope): description`, e.g. `feat(d1-netting): add firm-wide
netting`. Allowed types: `feat`, `fix`, `chore`, `docs`, `refactor`, `build`,
`ci`, `perf`, `style`, `test`, `revert`. A space is required after the colon.
One logical change per commit.

Note: this rule matches the `-m` form only. Commits opened in an editor or
via `-F` are not caught here — the local `pre-commit`/`pre-push` git hooks
are the backstop for those.
