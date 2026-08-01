# scripts/

- `gen-proto.sh`   — protoc → prost (Rust), protobuf (Python), ts-proto (TS), all via local plugin binaries (ADR-012). Outputs are committed.
- `schema-check.sh`— proto append-only lint (buf breaking) + Avro BACKWARD compatibility vs Schema Registry.
- `demo.sh`        — P1.M2 slice 3 (docs/ROADMAP.md): `just up`, wait for NATS, run `cargo test -p d1 --test nats_round_trip -- --ignored --nocapture` (publishes a `TargetPosition`, asserts an `ExecutionReport` reaching `Filled` comes back over real NATS + FIX). P1.M3 slice 3 added `cargo test -p d1 --test crosses_round_trip -- --ignored --nocapture` (two opposite `TargetPosition`s cross internally, asserting an `InternalCrossNotice` on `d1.crosses` plus a residual `ExecutionReport`; then an `InternalTransferRequest` books a directed cross the same way). P1.M4 slice 3 added a Kafka-readiness wait, provisioning of the four `posttrade.*` topics (`kafka-topics.sh --create --if-not-exists --partitions 1 --replication-factor 1`, done **before** the first `cargo test` above purely to provision once up front — only `golden_posttrade` actually wires a Kafka broker into `d1::spawn` and hard-errors at startup on a missing topic since `deploy/docker-compose.yml` disables auto-create; `nats_round_trip`/`crosses_round_trip` pass `kafka_brokers: None` and never touch Kafka), then `cargo test -p d1 --test golden_posttrade -- --ignored --nocapture` (drives the same crosses/transfers storyline with the Kafka broker wired in and `deterministic: true`, then diffs the resulting `posttrade.*` Avro records — decoded to JSON — against the committed golden files in `sim/golden/`; `UPDATE_GOLDEN=1` rewrites them instead of asserting). Then `just down`. Exits non-zero (containers left up for postmortem) if NATS/Kafka never comes up or any of the three round trips fails. Deferred, not implemented here: `sim`'s tracker-flow scenario replay (`sim/replay.rs` is still M1-scoped and never feeds `d1`) and leaving the UI running (Phase 3) — neither is pretended done.

`gen-proto.sh`/`schema-check.sh` are implemented in P1.M1; `demo.sh` in P1.M2 slice 3 (see docs/ROADMAP.md). The interfaces above are the contract.

## git-hooks/

Committed local git hooks enforcing that `main`/`master` never takes a direct commit or push (CLAUDE.md repo conventions — everything goes via a `type/scope-description` branch and a PR).

- `pre-commit` — refuses `git commit` when `HEAD` is `main`/`master`.
- `pre-push`   — refuses a push whose target ref is `main`/`master`.

One-time setup per clone: `just setup-hooks` (runs `git config core.hooksPath scripts/git-hooks`). This is a local safety net, not a substitute for GitHub branch protection on `main`.
