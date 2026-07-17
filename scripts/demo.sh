#!/usr/bin/env bash
# Contract: scripts/README.md. P1.M2 slice 3 (docs/ROADMAP.md): the current
# demo is the live NATS round trip (TargetPosition -> order -> fill ->
# ExecutionReport over the real NATS + FIX planes). Deferred pieces, by
# milestone -- not implemented here, not pretended:
#   - real netting/internal crosses (target_to_order's P1.M3 replacement)
#   - Kafka golden-file diff + UI left running (the eventual justfile
#     comment's "tracker-flow -> golden Kafka -> UI" contract): P1.M4 / Phase 3
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

cd delta-one
cargo test -p d1 --test nats_round_trip -- --ignored --nocapture
cd ..

just down
