# scripts/

- `gen-proto.sh`   — protoc → prost (Rust), protobuf (Python), ts-proto (TS). Outputs are committed.
- `schema-check.sh`— proto append-only lint (buf breaking) + Avro BACKWARD compatibility vs Schema Registry.
- `demo.sh`        — full end-to-end demo: infra up, run `sim` tracker-flow scenario, diff posttrade Kafka output vs `sim/golden/`, keep UI running.

These are implemented in P1.M1 (see docs/ROADMAP.md); the interfaces above are the contract.
