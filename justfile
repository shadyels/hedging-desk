# Desk Hedging Platform — task runner (ADR-007)
set shell := ["bash", "-cu"]

default:
    @just --list

# --- repo setup ----------------------------------------------------------
setup-hooks:
    chmod +x scripts/git-hooks/*
    git config core.hooksPath scripts/git-hooks

# --- infra -------------------------------------------------------------
up:
    docker compose -f deploy/docker-compose.yml up -d

down:
    docker compose -f deploy/docker-compose.yml down -v

# --- codegen -----------------------------------------------------------
proto:
    ./scripts/gen-proto.sh   # regenerates prost (Rust), protobuf (Python), ts-proto (TS); commit outputs

schema-check:
    ./scripts/schema-check.sh  # proto field-number lint + Avro BACKWARD compat vs registry

# --- delta one (Rust) ----------------------------------------------------
d1:
    cd delta-one && cargo run -p d1-core

d1-release:
    cd delta-one && cargo build --release

bench:
    cd delta-one && cargo bench   # release profile enforced by criterion config; HDR histograms to target/bench

# --- exo (Python) --------------------------------------------------------
exo:
    cd exo && uv run python -m exo

# --- ui / sim ------------------------------------------------------------
ui:
    cd ui && npm run dev

sim scenario="tracker-flow":
    cd delta-one && cargo run -p sim -- --scenario ../sim/scenarios/{{scenario}}.yaml

# --- quality gates -------------------------------------------------------
test:
    cd delta-one && cargo test
    cd exo && uv run pytest
    cd ui && npm test -- --run

lint:
    cd delta-one && cargo clippy --all-targets -- -D warnings && cargo fmt --check
    cd exo && uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src
    cd ui && npx tsc --noEmit && npx eslint src

# --- the showcase ---------------------------------------------------------
demo:
    ./scripts/demo.sh   # up -> sim tracker-flow -> assert golden Kafka output -> leave UI running
