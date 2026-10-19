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
