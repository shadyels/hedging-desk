---
name: block-workflow-edits-bash
enabled: true
event: bash
action: block
pattern: (>>?|sed\s+-i|tee|rm|mv|cp|install|truncate)[^\n]*\.github/workflows/
---

CI/CD workflows are protected. This shell command appears to write to, move,
copy, or delete a file under `.github/workflows/`.

Workflow changes must go through a normal branch + PR, not a direct shell
edit. Open a `type/scope-description` branch and let CI/a human review the
change.
