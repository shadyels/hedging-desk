"""Scaffold sanity: the package imports and config parses. Replaced by real tests from P2.M1."""

import tomllib
from pathlib import Path

import exo  # noqa: F401  (import must succeed once uv sync installs the package)


def test_exo_toml_parses() -> None:
    cfg = tomllib.loads((Path(__file__).parents[1] / "exo.toml").read_text())
    assert cfg["greeks"]["max_rel_std_error"] > 0
