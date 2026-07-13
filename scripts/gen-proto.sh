#!/usr/bin/env bash
# Contract: scripts/README.md. Implemented in P1.M1 (docs/ROADMAP.md).
#
# All codegen runs against local plugin binaries (no BSR remote plugins,
# no network dependency, no rate limits):
#   - Rust (prost):   cargo install protoc-gen-prost --version 0.5.0
#   - Python + .pyi:  protoc's own built-in generators (protoc >= 29, to
#                      match the protobuf==5.29.* runtime pinned in
#                      exo/pyproject.toml)
#   - TS (ts-proto):  already a ui/ devDependency; `npm install` in ui/
#                      installs its protoc-gen-ts_proto binary
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

missing=0
for bin in buf protoc protoc-gen-prost; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "scripts/gen-proto.sh: '$bin' not found on PATH." >&2
    missing=1
  fi
done
if [[ ! -x ui/node_modules/.bin/protoc-gen-ts_proto ]]; then
  echo "scripts/gen-proto.sh: ui/node_modules/.bin/protoc-gen-ts_proto not found — run 'npm install' in ui/ first." >&2
  missing=1
fi
if [[ "$missing" -ne 0 ]]; then
  cat >&2 <<'EOF'
Install hints:
  buf              https://buf.build/docs/installation (e.g. 'brew install bufbuild/buf/buf')
  protoc           needs to match protobuf==5.29.* in exo/pyproject.toml (protoc 29.x)
                   e.g. 'brew install protobuf@29' or download from
                   https://github.com/protocolbuffers/protobuf/releases
  protoc-gen-prost 'cargo install protoc-gen-prost --version 0.5.0'
EOF
  exit 1
fi

cd protocol
buf generate

mkdir -p ../exo/src/exo/bus/gen
protoc \
  -I proto \
  --python_out=../exo/src/exo/bus/gen \
  --pyi_out=../exo/src/exo/bus/gen \
  proto/common.proto proto/live.proto
