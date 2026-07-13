#!/usr/bin/env bash
# Contract: scripts/README.md. Implemented in P1.M1 (docs/ROADMAP.md).
#
# Two independent gates:
#   1. Proto: append-only breaking-change lint (buf breaking) vs main.
#   2. Avro:  BACKWARD compatibility vs Schema Registry, comparing the
#      working tree's .avsc against the version on main (registered into
#      the registry as the baseline), mirroring what buf breaking does for
#      proto. Requires `just up` (localhost:8081) to be running.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v buf >/dev/null 2>&1; then
  echo "scripts/schema-check.sh: 'buf' not found on PATH." >&2
  echo "Install it: https://buf.build/docs/installation (e.g. 'brew install bufbuild/buf/buf')" >&2
  exit 1
fi

# Baseline ref: prefer the up-to-date remote-tracking branch (CI does
# `git fetch origin main` on a shallow checkout); fall back to a local main.
base_ref=""
for ref in origin/main main; do
  if git rev-parse --verify -q "$ref" >/dev/null; then
    base_ref="$ref"
    break
  fi
done
if [[ -z "$base_ref" ]]; then
  echo "scripts/schema-check.sh: no 'origin/main' or 'main' ref found — fetch main first." >&2
  exit 1
fi

echo "== proto: append-only breaking-change check (vs main) =="
buf breaking protocol/proto --against ".git#branch=main,subdir=protocol/proto"

echo "== avro: BACKWARD compatibility vs Schema Registry (vs $base_ref) =="
registry=http://localhost:8081

echo "waiting for schema registry at $registry ..."
ready=0
for _ in $(seq 1 30); do
  if curl -sf "$registry/subjects" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" -ne 1 ]]; then
  echo "scripts/schema-check.sh: schema registry not reachable at $registry — run 'just up' first." >&2
  exit 1
fi

# avsc filename -> Kafka topic (ADR-002 topic map + tracker_analytics per
# delta-one/CLAUDE.md "Outbound planes" #5). Subject = <topic>-value
# (Schema Registry default TopicNameStrategy).
declare -A topic_for=(
  [posttrade_trade.avsc]=posttrade.trades
  [posttrade_cross.avsc]=posttrade.crosses
  [posttrade_allocation.avsc]=posttrade.allocations
  [order_audit.avsc]=posttrade.orders.audit
  [tracker_analytics.avsc]=posttrade.tracker.analytics
)

status=0
for avsc in protocol/avro/*.avsc; do
  file=$(basename "$avsc")
  topic="${topic_for[$file]:-}"
  if [[ -z "$topic" ]]; then
    echo "scripts/schema-check.sh: no topic mapping for $file — add one to topic_for in this script." >&2
    status=1
    continue
  fi
  subject="${topic}-value"

  baseline=$(git show "$base_ref:protocol/avro/$file" 2>/dev/null) || {
    echo "skip: $file is new on this branch (no $base_ref baseline to check against)"
    continue
  }

  # Register the main-branch schema as the compatibility baseline, then
  # check whether the working tree's version is BACKWARD-compatible with it.
  curl -sf -X POST \
    -H "Content-Type: application/vnd.schemaregistry.v1+json" \
    --data "$(printf '%s' "$baseline" | python3 -c 'import json,sys; print(json.dumps({"schema": sys.stdin.read()}))')" \
    "$registry/subjects/$subject/versions" >/dev/null

  candidate=$(cat "$avsc")
  response=$(curl -sf -X POST \
    -H "Content-Type: application/vnd.schemaregistry.v1+json" \
    --data "$(printf '%s' "$candidate" | python3 -c 'import json,sys; print(json.dumps({"schema": sys.stdin.read()}))')" \
    "$registry/compatibility/subjects/$subject/versions/latest")

  is_compatible=$(printf '%s' "$response" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("is_compatible", False))')
  if [[ "$is_compatible" != "True" ]]; then
    echo "FAIL: $file is not BACKWARD-compatible with subject $subject: $response" >&2
    status=1
  else
    echo "ok: $file ($subject)"
  fi
done

exit "$status"
