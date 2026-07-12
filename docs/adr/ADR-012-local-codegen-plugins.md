# ADR-012: Local plugin binaries for protobuf codegen (not BSR remote plugins)

**Status:** Accepted
**Date:** 2026-07-12
**Deciders:** desk lead (local plugins confirmed after hitting BSR rate limits during P1.M1 slice 2 implementation)

## Context

P1.M1 slice 2 wires up `scripts/gen-proto.sh` and `scripts/schema-check.sh` (ADR-007). `buf` drives both codegen (`buf generate`) and the proto append-only breaking-change lint (`buf breaking`); that split is unchanged by this ADR.

The first working version of `gen-proto.sh` generated all three languages through Buf Schema Registry (BSR) **remote plugins** — `buf.build/community/neoeinstein-prost`, `buf.build/protocolbuffers/python` (+ `pyi`), `buf.build/community/stephenh-ts-proto` — specifically so `just proto` would need no local protoc/plugin installs.

That had two real costs, discovered while wiring it up rather than in theory:

- Unauthenticated BSR remote codegen is capped at 10 requests/hour (960/hour once logged in via `buf registry login`). Ordinary iteration (a few `buf generate` runs while getting the config right) burns through that in minutes.
- It makes `buf registry login` — a BSR account — a practical requirement for anyone regenerating code more than a couple of times per hour. That's an external-service dependency the project doesn't otherwise have: ADR-007's whole premise is that generated code is committed and *builds* never need protoc; codegen is the only time any of this tooling matters, and it shouldn't need a third-party account to be usable.

## Options considered

- **BSR remote plugins** (the original slice-2 draft above): zero local plugin installs, but a hard external rate limit and a de facto login requirement. Rejected.
- **Local plugin binaries, still invoked through `buf generate`:** `protoc-gen-prost` (`cargo install protoc-gen-prost --version 0.5.0`) and `protoc-gen-ts_proto` (already a `ui/` devDependency — `npm install` installs its binary at `ui/node_modules/.bin/protoc-gen-ts_proto`) are real, redistributable `protoc-gen-*` plugin binaries. `protocol/buf.gen.yaml` points at them with `local:` instead of `remote:`. No network call, no rate limit, no account, and `buf breaking`/`buf lint` were never BSR-dependent in the first place (they operate on local git refs).
- **Python + `.pyi`:** protoc's Python and pyi generators are built into the `protoc` binary itself — there is no separate, redistributable `protoc-gen-python` for buf to shell out to locally (BSR's remote plugin of the same name works only because BSR runs actual `protoc` on its own servers). Generated with a direct `protoc --python_out=... --pyi_out=...` call in `scripts/gen-proto.sh`, run alongside (not through) `buf generate`. This requires a local `protoc` whose version line matches the `protobuf==5.29.*` runtime pinned in `exo/pyproject.toml` — protobuf's gencode/runtime version guard (`google.protobuf.internal.runtime_version`) refuses to load gencode newer than the installed runtime, so `protoc` must be line 29.x, not whatever is newest. (`brew install protobuf@29` if the system `protoc` is a different line.)

## Decision

`scripts/gen-proto.sh` uses **local plugin binaries only**, no BSR remote plugins:

- Rust: `protoc-gen-prost`, via `buf generate` (`protocol/buf.gen.yaml`, `local:` entry), output flattened with `flat_output_dir=true`.
- TypeScript: `protoc-gen-ts_proto` (from `ui/node_modules/.bin/`), via `buf generate`, `forceLong=bigint`.
- Python + `.pyi`: direct `protoc --python_out=... --pyi_out=...` call, protoc pinned to the 29.x line to match `protobuf==5.29.*`.

`buf` itself is unaffected and still required — it still drives this local-plugin `buf generate` and the `buf breaking` proto lint in `scripts/schema-check.sh`. Only the *BSR remote plugin service* is out of the picture; `buf`-the-CLI needs no login and makes no network calls for anything in this repo.

Contributors regenerating code need, once, locally: `buf`, a 29.x-line `protoc`, `protoc-gen-prost` (`cargo install`), and `ui/` with `npm install` already run. `scripts/gen-proto.sh` checks for all of these up front and prints install hints if any are missing.

A consequence worth flagging explicitly: because a Rust module's cross-package references (`super::super::common::v1::Meta`) are generated relative to the proto package path, the `pb` module in `delta-one/crates/d1-gateway-nats/src/lib.rs` nests as `pb::hedging::common::v1` / `pb::hedging::live::v1` (mirroring the `hedging.common.v1` / `hedging.live.v1` proto packages exactly), not a flatter `pb::common` / `pb::live`. Any future proto package rename must update that nesting to match.

## Consequences

- Easier: `just proto` works fully offline, with no BSR account, no rate-limit failures; regeneration is reproducible regardless of buf.build's availability.
- Harder: one more local tool per language to install once (`protoc`, `protoc-gen-prost`) instead of zero, and the `protoc`/`protobuf` runtime version pairing is a real constraint contributors must respect — documented here and in `scripts/gen-proto.sh`'s error output — or generated Python code fails to import at runtime (`VersionError`, not a silent bug).
