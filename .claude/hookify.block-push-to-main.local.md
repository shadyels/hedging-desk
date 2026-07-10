---
name: block-push-to-main
enabled: true
event: bash
action: block
pattern: git\s+push\b[^\n]*\b(?:main|master)\b|git\s+push\s+(?:-f|--force)
---

Direct pushes to `main`/`master`, and force-pushes of any kind, are blocked.

All changes must land on `main` via a `type/scope-description` branch and a
reviewed PR. If you need to update a branch history, coordinate with the user
before force-pushing anything.
