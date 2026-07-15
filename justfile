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
# P1.M2 slice 2: places one CLI-driven startup order over FIX (stand-in for
# the netting-driven emit that lands in P1.M3). Run `just sim-acceptor` in
# another terminal first.
d1 book="1" instrument="1001" side="buy" qty="10000" px="0":
    cd delta-one && cargo run -p d1 -- --book {{book}} --instrument {{instrument}} --side {{side}} --qty {{qty}} --px {{px}}

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

# sim's FIX acceptor counterparty for `just d1` (P1.M2 slice 2), the
# counterparty `d1-gateway-fix/initiator.cfg` connects to. fill_model:
# immediate|partial|reject. Run before `just d1`.
sim-acceptor fill_model="immediate":
    cd delta-one && cargo run -p sim -- --mode acceptor --fill-model {{fill_model}}

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
