#!/usr/bin/env python3
"""PreToolUse guard for Bash: branch naming, commit convention, push-to-main,
.env access, .github/workflows edits. Migrated from hookify.*.local.md rules.
"""
import json
import re
import sys

RULES = [
    (
        "enforce-branch-naming",
        re.compile(
            r"git\s+(?:checkout\s+-b|switch\s+-c|branch)\s+"
            r"(?!(?:feat|fix|chore|docs|refactor|d1|exo|ui|protocol|sim)/)[^\s]+",
            re.IGNORECASE,
        ),
        "Branch name doesn't follow repo convention (CLAUDE.md).\n\n"
        "Branch names must be `type/scope-description`, using one of: `feat`, `fix`, `chore`, `docs`, "
        "`refactor` (workflow types) or `d1`, `exo`, `ui`, `protocol`, `sim` (component names) as the "
        "`type/` prefix, kept under 50 characters, e.g. `feat/d1-netting`, `fix/exo-calibration`, "
        "`docs/adr-008`. Rename the branch to match and retry.",
    ),
    (
        "enforce-commit-convention",
        re.compile(
            r"git\s+commit\b[^\n]*?-m\s+"
            r"(?![\"']?(?:feat|fix|chore|docs|refactor|build|ci|perf|style|test|revert)(?:\([^)]+\))?!?:\s)",
            re.IGNORECASE,
        ),
        "Commit message doesn't follow Conventional Commits (CLAUDE.md).\n\n"
        "Use `type(scope): description`, e.g. `feat(d1-netting): add firm-wide netting`. Allowed types: "
        "`feat`, `fix`, `chore`, `docs`, `refactor`, `build`, `ci`, `perf`, `style`, `test`, `revert`. A "
        "space is required after the colon. One logical change per commit.\n\n"
        "Note: this rule matches the `-m` form only. Commits opened in an editor or via `-F` are not "
        "caught here — the local `pre-commit`/`pre-push` git hooks are the backstop for those.",
    ),
    (
        "block-push-to-main",
        re.compile(r"git\s+push\b[^\n]*\b(?:main|master)\b|git\s+push\s+(?:-f|--force)", re.IGNORECASE),
        "Direct pushes to `main`/`master`, and force-pushes of any kind, are blocked.\n\n"
        "All changes must land on `main` via a `type/scope-description` branch and a reviewed PR. If you "
        "need to update a branch history, coordinate with the user before force-pushing anything.",
    ),
    (
        "block-env-bash",
        re.compile(
            r"(?:cat|less|more|head|tail|vim|vi|nano|emacs|code|open|bat|sed|awk|grep|tee|cp|mv|>>?)"
            r"\s+[^\n]*\.env(?:\.[\w.-]+)?\b",
            re.IGNORECASE,
        ),
        "`.env` files are never read or written via shell commands by an agent. They hold local "
        "secrets/config.\n\n"
        "If you need to check whether a variable is set, ask the user to confirm rather than reading the "
        "file's contents.",
    ),
    (
        "block-workflow-edits-bash",
        re.compile(r"(>>?|sed\s+-i|tee|rm|mv|cp|install|truncate)[^\n]*\.github/workflows/", re.IGNORECASE),
        "CI/CD workflows are protected. This shell command appears to write to, move, copy, or delete a "
        "file under `.github/workflows/`.\n\n"
        "Workflow changes must go through a normal branch + PR, not a direct shell edit. Open a "
        "`type/scope-description` branch and let CI/a human review the change.",
    ),
]


def main():
    payload = json.load(sys.stdin)
    command = payload.get("tool_input", {}).get("command", "")

    for name, pattern, message in RULES:
        if pattern.search(command):
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                },
                "systemMessage": f"**[{name}]**\n{message}",
            }))
            return

    print(json.dumps({}))


if __name__ == "__main__":
    main()
