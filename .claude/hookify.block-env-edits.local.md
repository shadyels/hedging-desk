---
name: block-env-edits
enabled: true
event: file
action: block
conditions:
  - field: file_path
    operator: regex_match
    pattern: (^|/)\.env(\.[\w.-]+)?$
---

`.env` files are never edited by an agent. They hold local secrets/config
and are gitignored on purpose.

If a new environment variable is needed, add it to a checked-in example file
(e.g. `.env.example`) with a placeholder value and tell the user to set the
real value themselves.
