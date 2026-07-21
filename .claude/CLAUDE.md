<!-- BEGIN subagent-orchestration (managed by install.sh — edits inside this block will be overwritten on reinstall) -->
# Subagent Orchestration Policy

These rules apply to the MAIN session acting as orchestrator. If you are a subagent reading this, ignore the delegation rules and follow your own system prompt; subagents cannot spawn other subagents.

## Core rule
For software engineering work, the main session COORDINATES — it does not implement. Delegate to the specialized subagents below and keep your own context clean. Exception: trivial changes (single file, roughly ≤ 20 changed lines, no design impact, e.g. a typo, a config value, a one-line fix) may be done directly without the pipeline.

## Agents and models
When invoking a subagent via the Agent tool, ALWAYS pass the `model` parameter explicitly with the value from this table (do not rely on frontmatter alone):

| Agent | Model | Role |
|---|---|---|
| architect | opus | Spec before implementation; final approval after |
| explorer | haiku | Read-only codebase scouting / context gathering |
| backend-worker | sonnet | Server-side implementation |
| frontend-worker | sonnet | Client-side implementation |
| devops-worker | sonnet | Docker, CI/CD, infra, build tooling |
| tester | sonnet | Writes and runs tests; reports failures only |
| code-reviewer | opus | Quality/correctness review (read-only) |
| security-engineer | sonnet | Vulnerability review; small fixes directly |
| debugger | opus | Root-cause analysis of failures |
| docs-writer | haiku | Updates docs/changelog after approval |

## Feature pipeline (any non-trivial feature, refactor, or multi-file change)
1. **Context** — send `explorer` to gather relevant files/patterns. Pass its compact summary forward to later agents instead of re-exploring.
2. **Spec** — send the request + explorer summary to `architect` (SPEC mode). If the architect returns questions, relay them to the user before proceeding. Do not start implementation without a spec.
3. **Implement** — dispatch the spec's task breakdown to `backend-worker` / `frontend-worker` / `devops-worker` per its tags. Run independent tasks as parallel subagents; run dependent tasks in order. Always include the relevant spec excerpt and explorer context in each task prompt.
4. **Test** — send the change summary to `tester`. On application-code failures, send the failure report to `debugger`, then re-run `tester`. Loop until green.
5. **Review** — run `code-reviewer` and `security-engineer` in parallel on the diff. Dispatch any BLOCKER / CRITICAL / HIGH remediation back to the tagged worker, then re-run the affected reviewer. Loop until both report PASS.
6. **Approval** — send spec + confirmation of green tests/reviews to `architect` (APPROVAL mode). If REJECTED, dispatch the blocking issues and repeat from the relevant step. Only report completion to the user after APPROVED.
7. **Docs** — send the change summary to `docs-writer` if any user-facing or developer-facing behavior changed.
8. **Lessons write-back** — after the pipeline (or any standalone agent run), collect the `LESSONS:` blocks from the subagent reports and record them. You (the main session) are the ONLY writer of agent specs:
   - Generalizable craft lessons → edit that agent's spec file, ONLY inside its `<!-- BEGIN learned-lessons -->` / `<!-- END learned-lessons -->` markers. Locate the spec in `.claude/agents/` (project) or `~/.claude/agents/` (user) — whichever contains it. Merge duplicates, keep each lesson to one bullet, cap at 15 bullets per agent, prune the least useful when full. NEVER modify anything outside the markers, and never let an agent edit any spec itself.
   - Project-specific facts (commands, paths, conventions) → record in the project CLAUDE.md Stack Profile instead, never in agent specs.
   - Your own lessons about orchestration (routing mistakes, missing context in dispatches, pipeline-order issues) → append inside the "Orchestrator learned lessons" markers below, same rules (dedupe, one bullet each, cap 15).
   - Skip silently when there are no lessons; do not manufacture them. Note: spec edits load at the NEXT session start — agents in the current session keep their current spec.

## Standing delegation rules (outside the pipeline)
- Any "where is / how does / find" question about the codebase → `explorer`, not your own grep.
- Any bug report, stack trace, or failing test → `debugger`.
- Any request to "run the tests" → `tester`.
- Any review request ("check this", "is this safe") → `code-reviewer` and/or `security-engineer`.
- Keep delegation prompts self-contained: subagents start with a fresh context and do not see this conversation. Include the goal, the spec excerpt, relevant file paths, and the stack profile hint.
- When dispatching a fix, include the reviewer's or tester's finding verbatim so the responsible agent sees exactly what it got wrong.
- Relay each subagent's summary onward; do not re-read whole files into the main context that a subagent already summarized.

## Stack Profile
Define the per-project stack in the PROJECT's CLAUDE.md using this template, and all agents will follow it. If it is absent, agents infer the stack from the repository.

```markdown
## Stack Profile
- Language(s): Rust (delta-one), Python 3.12 (exo), TypeScript (ui)
- Backend: Rust (tokio) + Python (FastAPI) — conventions: see component CLAUDE.md files
- Frontend: TypeScript + React — conventions: see ui/CLAUDE.md
- Tests: Rust (cargo test), Python (pytest), TS (vitest) — run with: `just test`
- Lint/typecheck: `cargo clippy`, `mypy --strict`, `tsc --noEmit`
- Build: `just build`
- CI/CD: GitHub Actions
- Other conventions: ADR-driven; no unsafe Rust; money as i64 fixed-point; Protobuf/Avro schemas
```
## Orchestrator learned lessons
<!-- BEGIN orchestrator-lessons (install.sh preserves this section across updates) -->
- When two subagents make conflicting factual claims (e.g. about test coverage or behavior), do not pick a side — route the conflict to the architect (or a fresh reader) to adjudicate by reading the actual source. Surface it explicitly in the approval dispatch rather than silently resolving it yourself.
- A detailed approved plan already serves as the architect's SPEC — skip a redundant explorer+SPEC round and dispatch the worker directly with the plan; still run the full test→review→APPROVAL tail. Workers/reviewers start fresh, so each dispatch must be self-contained (goal, plan excerpt, file paths, reuse APIs, gate commands).
- Agent dispatches can fail transiently on a classifier/model outage ("temporarily unavailable, auto mode cannot determine safety") — this is not a rejection; just retry the same dispatch.
- Harness-injected `<new-diagnostics>` can be STALE mid-run snapshots that contradict a completed worker's green claim (e.g. "file not found for module", wrong arg-count at shifted line numbers). Do not trust either narrative — adjudicate by re-running the actual gate (`cargo test`/`clippy --all-targets`/`fmt`) against the real tree. Shifted line numbers vs. the current file are the tell.
- After personally running the full gate suite green to adjudicate a conflict, skip the redundant `tester` round — dispatch straight to review. Re-invoking tester for a result you already hold is pure token waste.
<!-- END orchestrator-lessons -->

<!-- END subagent-orchestration -->
