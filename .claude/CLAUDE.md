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
- When two subagents make conflicting factual claims (e.g. about test coverage or behavior), do not pick a side — route the conflict to the architect (or a fresh reader) to adjudicate by reading the actual source. Surface it explicitly in the approval dispatch rather than silently resolving it yourself. This covers conflicting **judgment** calls too, not just factual ones: two reviewers reaching opposite verdicts on the same lines (e.g. "add input bounds" vs "acceptable, single writer") is the archetypal architect question. Writing the disagreement into the PR body is disclosure, not adjudication — it does not discharge the obligation to actually run the gate.
- A detailed approved plan already serves as the architect's SPEC — skip a redundant explorer+SPEC round and dispatch the worker directly with the plan; still run the full test→review→APPROVAL tail. Workers/reviewers start fresh, so each dispatch must be self-contained (goal, plan excerpt, file paths, reuse APIs, gate commands).
- Harness-injected `<new-diagnostics>` can be STALE mid-run snapshots that contradict a completed worker's green claim (e.g. "file not found for module", wrong arg-count at shifted line numbers). Do not trust either narrative — adjudicate by re-running the actual gate (`cargo test`/`clippy --all-targets`/`fmt`) against the real tree. Shifted line numbers vs. the current file are the tell.
- Commit at every slice boundary the moment its gates go green. Once the next slice starts editing shared files the history can only be separated by `git hash-object` + `git update-index --cacheinfo` surgery — which works and leaves the working tree untouched, but is avoidable. Verify a split commit really builds by `git write-tree` + `git archive` into a scratch dir with its own `CARGO_TARGET_DIR`, never by assertion.
- A subagent dying on "session limit" or a mid-response connection drop is not a standing block on spawning — retry/resume before concluding you must do the work yourself. Resume by name: the agent keeps its context and only needs a precise statement of what remains.
- After any subagent crash, establish ground truth yourself with a real build (`cargo check --all-targets` + targeted greps) before resuming it. "File is modified" is not "spec item is done" — tell the resumed agent to re-audit what it believes it finished, since its own test run never executed. Audit each spec item against the CODE, and expect to be wrong in BOTH directions: in one post-crash audit a grep said an item was missing when it was implemented in a different function than I searched, and said another was present when the hits were docstrings rather than the guard itself. "Keyword present" is no more "done" than "file modified" is.
- Never trust a criterion number measured while other agents are running cargo. Load-normalise against a benchmark the change did not touch in the same run (ratio shifts survive contention, absolutes do not), and confirm the control's input construction sits outside the timed closure before believing it. Re-measure on a quiet machine against a named `--baseline` rather than criterion's default "previous run", which silently makes successive readings incomparable. The same distrust applies to resource-model CONSTANTS, which must be measured rather than hand-counted: a peak-memory constant here was wrong three times (2 -> 4 -> a measured 5), always understating, because each correction re-counted the arrays the function allocates and none saw the transient one layer down in a helper (`np.concatenate([base, mirror])` holding both halves and the result live). Mandate a `tracemalloc` measurement against the real call — and mandate that the REGRESSION TEST measure too, since a test that re-derives the model it checks will faithfully certify each successive wrong answer.
- Fact-check `docs-writer` output against the actual commits before committing it. In one pass it inverted the slice numbering and silently rewrote a roadmap exit criterion to describe what shipped — turning "not met" into "met" by redefinition. Doc agents optimise for plausible prose, not for correspondence with the diff.
- Reviewers who never saw the plan catch the orchestrator's own spec errors — a worker faithfully implements a wrong instruction and writes a matching comment, so the error is invisible until someone reads the code against the ADR instead of against the dispatch.
- `.claude/hooks/bash_guard.py`'s `enforce-commit-convention` inspects the raw `-m` argument, so the `git commit -m "$(cat <<'EOF' …)"` idiom is always denied — the regex sees `-m "$(cat`, not the Conventional Commits prefix. A worker reporting "a hook blocks git commit entirely" has misdiagnosed this. **Multiple `-m` flags are also always denied**: the rule is a `search` for `-m\s+(?!<prefix>)`, so it keeps scanning past a well-formed subject and matches at the second `-m`, whose body paragraph has no prefix. For anything longer than one line — bodies, `Co-Authored-By`/`Claude-Session` trailers — write the message to a scratchpad file and use `git commit -F <file>`, which the rule's own help text names as not caught (local `pre-commit` hooks remain the backstop). Reserve the literal single-line `-m` string (no `$`, no backticks, no inner `"`) for one-line commits.
- When the branch was cut from another *feature* branch rather than `main`, never hand a reviewer `git diff main...HEAD` — the three-dot diff drags in every commit since divergence and presents already-reviewed work as new. Check `git rev-list --count main..HEAD` before writing the dispatch, and give reviewers the precise command (`git diff HEAD -- <paths>` plus any untracked files by name). Also tell the user, since it changes what the PR should target.
- A harness checkpoint ("usage limit approaching", "don't start subagents", "wrap up") is a budget signal, NOT user authorization to drop a pipeline gate — and a following one-word user reply like "finish" is not retroactive consent to whatever I decided to cut. If budget is genuinely tight, ask which gate to drop or defer the work; do not silently pick. The APPROVAL round is the worst one to cut, because it is the only step that judges the ORCHESTRATOR's own calls — scope declines, tie-breaks between reviewers, doc edits I wrote myself. Re-running tests personally substitutes for `tester`; nothing I can run substitutes for a fresh reader checking my judgment. Disclosing the skip afterwards does not repair it.
- An approved plan is a spec, not a source of facts. Verify every count, path and line reference it asserts before propagating it into a doc or a dispatch — a plan of mine claimed a scenario had "ten timeline entries" and "five `expect:` entries" when the file had eight and four, and the wrong number was already written into `ROADMAP.md` before I checked. The plan's own reasoning survived; only the numbers were wrong, which is exactly the failure that slips through. Counting by grep is the same trap: enumerating `ponytail:` markers missed one written `ponytail (P0-3, ...)` with no colon, my count said 6 and a reviewer's said 10 (it counted prose references as definitions), and the repo ledger's own headline was already wrong on `main`. Grep the BARE keyword, classify hits into definitions vs references, and confirm a baseline on `main` before attributing a bad count to your diff.
- Freeze the working tree for the WHOLE duration of any run that captures provenance — no edits, no deletions, not even tidying. A long study calls `git_sha()` at the END of its run, so deleting a COMMITTED artifact "so a stale one cannot linger" dirties the tree and stamps the new artifact `-dirty`, corrupting exactly the provenance the run exists to establish. A committed artifact is meant to be overwritten in place; if the run fails, `git checkout` restores it in one command, so the pre-deletion can only lose. State this as an absolute, not a judgment call: I applied it correctly once (declining to fix a comment mid-run) and violated it 30 minutes later with an `rm` I had reasoned myself into as cleanup.
- A selection criterion must be specified against a quantity the independent variable can actually MOVE, and its degenerate cases must raise rather than fall back. I gated convergence on each row's raw `se` below an absolute tolerance, but `se` is set by payoff variance and does not depend on step count, so refining steps could never satisfy it — nothing passed, and a wall-time fallback silently became the entire decision, reporting a stopwatch reading as a convergence result. The fallback was the worse defect: a broken criterion that returns a CONFIDENT WRONG ANSWER instead of an error is indistinguishable from a real finding. Require both degenerate cases to raise — no candidate passes, and ALL candidates pass trivially — since they are the same failure from opposite sides.
<!-- END subagent-orchestration -->
