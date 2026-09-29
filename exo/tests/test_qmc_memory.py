"""Regression test for `qmc_variance_reduction._PEAK_ARRAY_RATIO` (T8).

MEASURES peak memory against the real `price_rqmc()`, never re-derives the model it checks (see
`_PEAK_ARRAY_RATIO`'s own comment in the study module for why: a hand re-count of heston.py's
arrays got that module's own constant wrong twice, missing a transient array one layer down).
This test exercises the exact configuration `_PEAK_ARRAY_RATIO` was measured at, so a regression
in `price_rqmc`'s allocation pattern (a new transient array, a bigger point matrix) fails this
test rather than silently going unnoticed until a real sweep runs out of memory.
"""

from __future__ import annotations

import tracemalloc

from exo.models.params import EngineConfig, HestonParams
from exo.products.barrier import BarrierOption
from exo.products.base import Monitoring
from exo.products.pricer import price_rqmc
from exo.studies.qmc_variance_reduction import _PEAK_ARRAY_RATIO

_PARAMS = HestonParams(
    s0=187.50, r=0.0425, q=0.0050, v0=0.0400, kappa=1.50, theta=0.0400, xi=0.60, rho=-0.70
)
_STRIKE = 187.50
_N_PATHS = 1024
_N_STEPS = 50


def _down_and_out_call() -> BarrierOption:
    return BarrierOption(
        underlying="AAPL",
        option_type="call",
        strike=_STRIKE,
        expiry=1.0,
        barrier=0.9 * _STRIKE,
        direction="down",
        knock="out",
        monitoring=Monitoring.CONTINUOUS_BRIDGE,
    )


def test_peak_array_ratio_covers_measured_peak_with_tracemalloc() -> None:
    """The worst-case config this study runs: RQMC + bridge + control variate (config 5's
    shape), n_paths=1024, n_steps=50, scheme=qe. A warmup call runs first, OUTSIDE the timed
    window, so scipy's one-time lazy-import cost (scipy.stats.qmc, scipy.special.ndtri) is not
    counted as if it were this call's own peak."""
    engine = EngineConfig(
        scheme="qe", n_steps=_N_STEPS, n_paths=_N_PATHS, expiry=1.0, antithetic=False
    )

    price_rqmc(
        _down_and_out_call(),
        _PARAMS,
        engine,
        seed=1,
        n_replicates=8,
        bridge=frozenset({"spot"}),
        control_strike=_STRIKE,
    )  # warmup, untimed

    tracemalloc.start()
    price_rqmc(
        _down_and_out_call(),
        _PARAMS,
        engine,
        seed=42,
        n_replicates=32,
        bridge=frozenset({"spot"}),
        control_strike=_STRIKE,
    )
    _current, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    ceiling = _PEAK_ARRAY_RATIO * _N_PATHS * (_N_STEPS + 1) * 8
    assert peak_bytes <= ceiling, (
        f"peak_bytes={peak_bytes} exceeds the committed ceiling {ceiling} "
        f"(_PEAK_ARRAY_RATIO={_PEAK_ARRAY_RATIO}) -- re-measure and update the constant"
    )
