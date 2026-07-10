# ADR-007: Monorepo layout and toolchain

**Status:** Accepted **Date:** 2026-07-05 **Deciders:** desk lead (monorepo confirmed)

## Decision

- Single repo; top-level dirs = deployable components (`delta-one/`, `exo/`, `ui/`, `sim/`) + shared contracts (`protocol/`) + `docs/` + `deploy/`.
- Task runner: `just` (uniform entry points across three toolchains; chosen over Make on the stated "prefer the newer of two viable options" policy).
- Rust: single Cargo workspace under `delta-one/` (sim's Rust binaries join it), pinned toolchain via `rust-toolchain.toml`, clippy lint policy in workspace `Cargo.toml`.
- Python: `uv` + `pyproject.toml`, `ruff`, `mypy --strict`, `pytest`.
- TS: Vite + `tsc --noEmit` + eslint/prettier + Vitest.
- Codegen: `just proto` regenerates prost/protobuf-py/ts-proto outputs; generated code committed.
- CI: per-component jobs keyed on changed paths + always-on schema-compat job.
- Local infra: `deploy/docker-compose.yml` (NATS with WS listener, Kafka KRaft, Schema Registry).

## Consequences

- Easier: one clone = whole demo; contracts and code move in one PR; agents navigate via per-dir CLAUDE.md files.
- Harder: CI path-filtering discipline; three toolchains in one CI image.
