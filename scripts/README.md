# scripts/

- `gen-proto.sh`   — protoc → prost (Rust), protobuf (Python), ts-proto (TS). Outputs are committed.
- `schema-check.sh`— proto append-only lint (buf breaking) + Avro BACKWARD compatibility vs Schema Registry.
- `demo.sh`        — full end-to-end demo: infra up, run `sim` tracker-flow scenario, diff posttrade Kafka output vs `sim/golden/`, keep UI running.

These are implemented in P1.M1 (see docs/ROADMAP.md); the interfaces above are the contract.

## git-hooks/

Committed local git hooks enforcing that `main`/`master` never takes a direct commit or push (CLAUDE.md repo conventions — everything goes via a `type/scope-description` branch and a PR).

- `pre-commit` — refuses `git commit` when `HEAD` is `main`/`master`.
- `pre-push`   — refuses a push whose target ref is `main`/`master`.

One-time setup per clone: `just setup-hooks` (runs `git config core.hooksPath scripts/git-hooks`). This is a local safety net, not a substitute for GitHub branch protection on `main`.
