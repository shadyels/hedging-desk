"""Measured peak-memory constants for the path-retaining engines (ADR-006 Amendment 7 s1).

Each constant is `peak_bytes / (n_paths * (n_steps + 1) * 8)`, i.e. peak bytes in units of ONE
`(n_paths, n_steps + 1)` float64 array, MEASURED with `tracemalloc` against the real call -- never
hand-counted: a hand re-count of heston.py's arrays got a constant wrong three times, always low,
by missing a transient array one layer down. The regression test for each constant measures the
same call and asserts `measured <= ceiling <= 1.06 * measured`.
"""

from __future__ import annotations

# Peak bytes per (path, step) for one `price_rqmc` call (up to three bundles and two point matrices
# live at peak -- the pilot plus the previous and current replicate; see price_rqmc's docstring),
# MEASURED with tracemalloc (not hand-counted -- see `test_qmc_memory.py`) at the worst-case config
# the QMC study actually runs: n_paths=1024, n_steps=50, scheme="qe", bridge={"spot"},
# control_strike set (config 5's shape). Measured peak 7_660_768 bytes against a denominator of
# n_paths*(n_steps+1)*8 = 417_792 bytes -> ratio ~18.34 (two repeated measurements agreed to 4
# significant figures after a warmup call to exclude scipy's one-time lazy-import cost from the
# trace). Committed at 19.0, a ~3.6% margin over the measured value -- covers heston.py's own ~5x
# array footprint PLUS SobolRandomSource's (n_paths, 2*n_steps) point matrices (pilot + current)
# PLUS bridge_normals' transient W array PLUS the control variate's own payoff/control arrays.
RQMC_PEAK_ARRAY_RATIO = 19.0

# Peak for one `price_american` call (products/pricer.py): pilot simulate -> fit -> pilot bundle
# DROPPED -> valuation simulate -> apply. MEASURED with tracemalloc against the real call at
# n_paths=20_000, n_steps=50, scheme="qe", antithetic (warm-up call outside the window): peak/array
# = 4.982 (4.982 on a repeat; 4.981 at 40_000 paths) -- exactly ONE bundle alive at peak, i.e.
# `simulate()`'s own transient footprint (heston.py's 5.02-5.08) and nothing on top; the fit/apply
# working set is small beside it. A kept pilot reference would read ~7. Committed at 5.2, a ~4.4%
# margin (the regression test asserts `measured <= ceiling <= 1.06 * measured`).
LSM_PEAK_ARRAY_RATIO = 5.2

# Peak for one `compute_greeks` call (greeks/bump.py; the load-time guard of ADR-006 Amendment 9
# s7): ten sequential `simulate()` legs, each reduced at once to a (n_paths,) vector, with only a
# ONE-column pairing stub of the base bundle kept -- so no leg holds more than one bundle (an
# earlier prototype that kept the whole base bundle and every leg measured 9.03 for the
# autocallable). MEASURED with tracemalloc against the real call (warm-up outside the trace), for
# every loadable class at 4_000 x 50 and 8_000 x 100, scheme="qe", antithetic
# (`tests/test_greeks_memory.py` repeats this): peak/array = 5.18 / 5.09 for every DISCRETE class
# (autocallable, discrete barrier/BRC/bonus, RC, warrant, TARF, PTARF) -- `simulate()`'s own
# transient footprint, as for `LSM_PEAK_ARRAY_RATIO`; the CONTINUOUS_BRIDGE classes (barrier, BRC,
# bonus) bind at 11.46 / 11.56 (4_000 x 50) and 11.41 / 11.46 (8_000 x 100), the `bridge_step_
# survival` temporaries. Cross-check at the production size, 20_000 x 250: autocallable 5.04 (202
# MB), continuous barrier 11.39 (457 MB), continuous BRC 11.41 (458 MB). Repeats agree to 3 d.p.
# Committed at 12.0, 3.8% over the worst measured ratio (11.557); the regression test asserts
# `measured <= ceiling <= 1.06 * max(measured)`. One constant for all classes: the discrete
# classes are over-estimated ~2.3x, which only makes the guard conservative.
GREEKS_PEAK_ARRAY_RATIO = 12.0
