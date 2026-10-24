"""Negative controls for the Greek gates: each mutant breaks `exo.greeks.bump` in one named way
(ADR-006 Amendment 9 s3's mutant list) and the gate MUST then fail. A gate that cannot fail
certifies nothing. The mutants patch the real `compute_greeks`; none re-implements it."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest
from _greeks_gate_helpers import DEGENERATE, Opt, run_greeks
from numpy.typing import NDArray
from test_validation_gates_greeks import _SEEDS, _gate_gk

import exo.greeks.bump as bump
from exo.greeks.bump import Greeks
from exo.models.estimator import PriceResult, mc_estimate
from exo.models.heston import PathBundle, simulate
from exo.models.params import EngineConfig, HestonParams
from exo.models.rng import PseudoRandomSource, RandomSource

# compute_greeks calls mc_estimate once per Greeks field, in field order.
_GREEK_ORDER = tuple(f.name for f in dataclasses.fields(Greeks))
_OPT: Opt = "call"
_SEED = _SEEDS[_OPT]


def _gate_fails(greeks: Greeks, names: tuple[str, ...]) -> str:
    with pytest.raises(AssertionError) as exc:
        _gate_gk(_OPT, greeks)
    msg = str(exc.value)
    assert any(f"{_OPT} {n}:" in msg for n in names), msg  # killed by the expected Greek
    return msg


def test_honest_estimator_passes_the_same_gate_the_mutants_are_run_through() -> None:
    _gate_gk(_OPT, run_greeks(_OPT, _SEED))


def test_mutant_vega_bumps_v0_only_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    real_with = bump._with

    def with_v0_only(params: HestonParams, **changes: float) -> HestonParams:
        changes.pop("theta", None)
        return real_with(params, **changes)

    monkeypatch.setattr(bump, "_with", with_v0_only)
    _gate_fails(run_greeks(_OPT, _SEED), ("vega",))


def test_mutant_rho_moves_only_the_discount_not_the_drift_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def sim_at_base_r(
        params: HestonParams, engine: EngineConfig, source: RandomSource
    ) -> PathBundle:
        return simulate(params.model_copy(update={"r": DEGENERATE.r}), engine, source)

    monkeypatch.setattr(bump, "simulate", sim_at_base_r)
    _gate_fails(run_greeks(_OPT, _SEED), ("rho",))


def test_mutant_theta_without_dropped_leading_step_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uncoupled theta: its SE inflates by the G-CRN factor (>= 3), so the `se < tol_abs` conjunct
    (and the G-CRN theta ratio) reject it."""
    honest = run_greeks(_OPT, _SEED)
    monkeypatch.setattr(bump, "DropLeadingStepsSource", lambda base, k=1: base)
    mutant = run_greeks(_OPT, _SEED)
    assert mutant.theta.std_err / honest.theta.std_err >= 3.0
    _gate_fails(mutant, ("theta",))


def test_mutant_broken_crn_each_leg_on_its_own_seed_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = iter(range(1, 1000))

    def sim_own_seed(
        params: HestonParams, engine: EngineConfig, source: RandomSource
    ) -> PathBundle:
        fresh = PseudoRandomSource(source.seed + 10_000 * next(calls), antithetic=True)
        return simulate(params, engine, fresh)

    honest = run_greeks(_OPT, _SEED)
    monkeypatch.setattr(bump, "simulate", sim_own_seed)
    mutant = run_greeks(_OPT, _SEED)
    for name in ("delta", "vega", "rho", "theta"):
        ratio = getattr(mutant, name).std_err / getattr(honest, name).std_err
        assert ratio > 3.0, (name, ratio)
    _gate_fails(mutant, ("delta", "vega", "rho", "theta"))


@pytest.mark.parametrize("index", [1, 2, 3, 4, 5])  # delta, gamma, vega, rho, dq
def test_mutant_greek_with_wrong_scale_is_caught(
    monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    """A Greek whose per-path combination is off by 1.5x (the 'gamma missing its S scale' family).
    SE scales with the value, so only the comparison against the closed form can kill it."""
    calls = 0

    def scaled(b: PathBundle, per_path: NDArray[np.float64]) -> PriceResult:
        nonlocal calls
        calls += 1
        return mc_estimate(b, per_path * (1.5 if calls - 1 == index else 1.0))

    monkeypatch.setattr(bump, "mc_estimate", scaled)
    name = _GREEK_ORDER[index]
    _gate_fails(run_greeks(_OPT, _SEED), (name,))
    assert calls == len(_GREEK_ORDER)  # one mc_estimate per Greek field, in field order


def test_mutant_theta_identically_zero_is_caught_by_the_se_conjunct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    theta_index = _GREEK_ORDER.index("theta")

    def zero_theta(b: PathBundle, per_path: NDArray[np.float64]) -> PriceResult:
        nonlocal calls
        calls += 1
        return mc_estimate(b, np.zeros_like(per_path) if calls - 1 == theta_index else per_path)

    monkeypatch.setattr(bump, "mc_estimate", zero_theta)
    msg = _gate_fails(run_greeks(_OPT, _SEED), ("theta",))
    assert calls == len(_GREEK_ORDER)
    assert "not a meaningful gate" in msg
