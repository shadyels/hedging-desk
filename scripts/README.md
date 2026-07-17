# scripts/

- `gen-proto.sh`   — protoc → prost (Rust), protobuf (Python), ts-proto (TS), all via local plugin binaries (ADR-012). Outputs are committed.
- `schema-check.sh`— proto append-only lint (buf breaking) + Avro BACKWARD compatibility vs Schema Registry.
- `demo.sh`        — P1.M2 slice 3 (docs/ROADMAP.md): `just up`, wait for NATS, run `cargo test -p d1 --test nats_round_trip -- --ignored --nocapture` (publishes a `TargetPosition`, asserts an `ExecutionReport` reaching `Filled` comes back over real NATS + FIX), `just down`. Exits non-zero (containers left up for postmortem) if NATS never comes up or the round trip fails. The eventual full storyline — `sim` tracker-flow scenario, posttrade Kafka diff vs `sim/golden/`, UI left running — lands with P1.M4/Phase 3 as netting (P1.M3) and post-trade (P1.M4) land; not implemented yet, not pretended here.

`gen-proto.sh`/`schema-check.sh` are implemented in P1.M1; `demo.sh` in P1.M2 slice 3 (see docs/ROADMAP.md). The interfaces above are the contract.

## git-hooks/

Committed local git hooks enforcing that `main`/`master` never takes a direct commit or push (CLAUDE.md repo conventions — everything goes via a `type/scope-description` branch and a PR).

- `pre-commit` — refuses `git commit` when `HEAD` is `main`/`master`.
- `pre-push`   — refuses a push whose target ref is `main`/`master`.

One-time setup per clone: `just setup-hooks` (runs `git config core.hooksPath scripts/git-hooks`). This is a local safety net, not a substitute for GitHub branch protection on `main`.
