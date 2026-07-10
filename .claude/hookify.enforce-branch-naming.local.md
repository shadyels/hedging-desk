---
name: enforce-branch-naming
enabled: true
event: bash
action: block
pattern: git\s+(?:checkout\s+-b|switch\s+-c|branch)\s+(?!(?:feat|fix|chore|docs|refactor|d1|exo|ui|protocol|sim)/)[^\s]+
---

Branch name doesn't follow repo convention (`CLAUDE.md`).

Branch names must be `type/scope-description`, using one of: `feat`, `fix`, `chore`, `docs`, `refactor` (workflow types) or `d1`, `exo`, `ui`, `protocol`, `sim` (component names) as the `type/` prefix, kept under 50 characters, e.g. `feat/d1-netting`, `fix/exo-calibration`, `docs/adr-008`. Rename the branch to match and retry.
