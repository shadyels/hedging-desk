---
name: block-workflow-edits
enabled: true
event: file
action: block
conditions:
  - field: file_path
    operator: regex_match
    pattern: \.github/workflows/
---

CI/CD workflows are protected. Do not edit files under `.github/workflows/` directly.

Workflow changes must go through a normal branch + PR so CI itself reviews the change before it takes effect. Open a `type/scope-description` branch, make the change there, and let a human review the PR.
