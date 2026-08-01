#!/usr/bin/env bash
# Contract: scripts/README.md. docs/ROADMAP.md P1.M2 slice 3 added the live
# NATS round trip (TargetPosition -> order -> fill -> ExecutionReport over
# the real NATS + FIX planes); P1.M3 slice 3 added the crosses/transfers
# round trip (two opposite TargetPositions -> InternalCrossNotice + residual
# ExecutionReport, plus an InternalTransferRequest -> InternalCrossNotice);
# P1.M4 slice 3 added the Kafka posttrade golden-file diff (the same
# crosses/transfers storyline, driven with a real Kafka broker wired in,
# diffed against sim/golden/).
# Deferred pieces, by milestone -- not implemented here, not pretended:
#   - sim tracker-flow scenario replay (sim's replay.rs is still M1-scoped
#     and never feeds d1) and UI left running (Phase 3): both land later.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

just up

echo "scripts/demo.sh: waiting for NATS on 127.0.0.1:4222..."
# curl against the monitoring port (deploy/docker-compose.yml: 8222) rather
# than a raw TCP probe of 4222 -- bash's /dev/tcp redirection isn't available
# in every shell build (observed: absent in one dev sandbox), while curl is
# universally present here and elsewhere in these scripts (none exist yet,
# but it's the obvious portable choice over a bash-only feature).
nats_up=0
for _ in $(seq 1 50); do
  if curl -sf http://127.0.0.1:8222/varz >/dev/null 2>&1; then
    nats_up=1
    break
  fi
  sleep 0.2
done
if [[ "$nats_up" -ne 1 ]]; then
  echo "scripts/demo.sh: NATS never came up on 127.0.0.1:4222 (8222/varz unreachable)" >&2
  exit 1
fi

echo "scripts/demo.sh: waiting for Kafka on 127.0.0.1:9092..."
kafka_up=0
for _ in $(seq 1 50); do
  if docker compose -f deploy/docker-compose.yml exec -T kafka \
      /opt/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server localhost:9092 \
      >/dev/null 2>&1; then
    kafka_up=1
    break
  fi
  sleep 0.2
done
if [[ "$kafka_up" -ne 1 ]]; then
  echo "scripts/demo.sh: Kafka never came up on 127.0.0.1:9092" >&2
  exit 1
fi

# Only golden_posttrade.rs wires a Kafka broker into d1::spawn (both
# nats_round_trip.rs and crosses_round_trip.rs pass kafka_brokers: None, no
# producer thread, no topic dependency); it hard-errors at startup if any of
# these four topics is missing (deploy/docker-compose.yml disables
# KAFKA_AUTO_CREATE_TOPICS_ENABLE). Provisioned here, before the first cargo
# test, purely so it's done once up front rather than reordering this loop
# around golden_posttrade specifically -- harmless for the two tests that
# don't touch Kafka. 1 partition / replication-factor 1 matches the
# single-broker compose and is what makes per-topic record order
# deterministic (P1.M4 slice 3's golden diff depends on that).
echo "scripts/demo.sh: provisioning posttrade.* topics..."
for topic in posttrade.trades posttrade.crosses posttrade.allocations posttrade.orders.audit; do
  docker compose -f deploy/docker-compose.yml exec -T kafka \
    /opt/kafka/bin/kafka-topics.sh --create --if-not-exists \
    --bootstrap-server localhost:9092 \
    --partitions 1 --replication-factor 1 \
    --topic "$topic"
done

cd delta-one
cargo test -p d1 --test nats_round_trip -- --ignored --nocapture
cargo test -p d1 --test crosses_round_trip -- --ignored --nocapture
cargo test -p d1 --test golden_posttrade -- --ignored --nocapture
cd ..

just down
