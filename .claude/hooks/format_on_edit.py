#!/usr/bin/env python3
"""PostToolUse formatter for Edit/Write: keeps formatting noise out of review.

Runs the same formatters as `just lint` (`cargo fmt` / `ruff format`) on the one
edited file. Never blocks: a missing tool or a formatter error is ignored, since
`just lint` remains the real gate.
"""
import json
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
try:
    path = Path(json.load(sys.stdin)["tool_input"]["file_path"]).resolve()
    rel = path.relative_to(root)
except (KeyError, ValueError, json.JSONDecodeError):
    sys.exit(0)

cmd, cwd = None, root
if rel.parts[0] == "delta-one" and path.suffix == ".rs":
    # Edition mirrors delta-one/Cargo.toml; rustfmt on a bare file does not read it.
    cmd = ["rustfmt", "--edition", "2024", str(path)]
elif rel.parts[0] == "exo" and path.suffix == ".py":
    # cwd=exo so pyproject's line-length applies; --force-exclude honours the
    # protoc-generated `bus/gen` exclusion even for an explicitly named file.
    cwd = root / "exo"
    cmd = [str(cwd / ".venv/bin/ruff"), "format", "--force-exclude", str(path)]

if cmd and path.exists():
    try:
        subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass
