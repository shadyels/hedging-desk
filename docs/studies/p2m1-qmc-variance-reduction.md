# P2.M1 Slice 3 -- QMC Variance-Reduction Study

> **Illustrative and UNCALIBRATED parameters (ADR-006 Amendment 4 s1 / Amendment 5).** The Heston parameter sets swept in this study are demo/development values, not calibrated market data, restricted to AAPL and MSFT (Amendment 5) -- SPX's Feller-0.231 regime is P2.M2's warrant-gate trigger and is not swept here.

**1/7 cell(s) INCONCLUSIVE** (see Cell results for the raw evidence each preserved) -- `rank_configs` raised rather than falling back to a wall-time or best-effort pick.

## Provenance

- base seed: `20260924`
- paths per replicate: `1024`
- git_sha: `8f9a75b27e366cb7628f2add4640598dda2c3a3c`
- params_hash: `26f5271c72ad778955c05a4582ac644e58459dad5c0cee6961aed1a4622c1261`
- run_id: `2026-09-24T19-44-54Z-qmc-variance-reduction`
- NOTE: `manifest.engine` reflects configuration 4 (RQMC+bridge, QE) only -- the manifest schema has one `[engine]` table; this study sweeps six configurations per cell (see Configurations compared / Cell results below).

## Measurement method

Formula SEs from `rqmc_estimate` are optimistic by a factor that grows with both replicate count and net size (heavy-tailed replicate-mean distribution); comparing them directly to pseudo-random formula SEs would inflate every RQMC ratio. Instead each (cell, configuration) runs `T` independent repetitions at the identical total path budget `B`, and `rmse = std(repetition pvs, ddof=1)` -- free of that bias -- is the error measure this study actually ranks on. The headline ratio is `rmse_denominator / rmse_config`; `mean_formula_se / rmse` (SE calibration) reports the bias actually measured on this run's own configuration.

## Configurations compared

| # | label | sampler | scope |
|---|---|---|---|
| 1 | pseudo | pseudo | all cells |
| 2 | pseudo+antithetic | pseudo | all cells |
| 3 | rqmc | rqmc | all cells |
| 4 | rqmc+bridge | rqmc | all cells |
| 5 | rqmc+bridge+cv | rqmc | barrier cell only |
| 6 | pseudo+antithetic+cv | pseudo | barrier cell only |

## Variance reduction achieved

Headline ratio `rmse_denominator / rmse_config` per cell/config, at T=32 repetitions (relative precision +/-13%, `1/sqrt(2*(T-1))`). The autocallable is the highest-dimensional, most discontinuous payoff swept here (early redemption truncates the path, unlike the barrier's smoother survival weight) -- RQMC's advantage is EXPECTED to be smaller on that cell than on vanilla, and that is reported as a finding, not adjusted toward a larger number.

| cell | config | ratio | best? |
|---|---|---|---|
| vanilla/AAPL/qe | pseudo | 1.00x +/- 13% |  |
| vanilla/AAPL/qe | pseudo+antithetic | 1.41x +/- 13% |  |
| vanilla/AAPL/qe | rqmc | 2.96x +/- 13% | <- BEST |
| vanilla/AAPL/qe | rqmc+bridge | 2.83x +/- 13% |  |
| vanilla/MSFT/qe | pseudo | 1.00x +/- 13% |  |
| vanilla/MSFT/qe | pseudo+antithetic | 1.55x +/- 13% |  |
| vanilla/MSFT/qe | rqmc | 2.87x +/- 13% |  |
| vanilla/MSFT/qe | rqmc+bridge | 5.53x +/- 13% | <- BEST |
| barrier/AAPL/qe | pseudo | 1.00x +/- 13% |  |
| barrier/AAPL/qe | pseudo+antithetic | 1.70x +/- 13% |  |
| barrier/AAPL/qe | rqmc | 1.62x +/- 13% |  |
| barrier/AAPL/qe | rqmc+bridge | 2.11x +/- 13% |  |
| barrier/AAPL/qe | rqmc+bridge+cv | 2.41x +/- 13% | <- BEST |
| barrier/AAPL/qe | pseudo+antithetic+cv | 2.15x +/- 13% |  |
| barrier/MSFT/qe | pseudo | 1.00x +/- 13% |  |
| barrier/MSFT/qe | pseudo+antithetic | 1.41x +/- 13% |  |
| barrier/MSFT/qe | rqmc | 2.40x +/- 13% |  |
| barrier/MSFT/qe | rqmc+bridge | 3.24x +/- 13% | <- BEST |
| barrier/MSFT/qe | rqmc+bridge+cv | 2.88x +/- 13% |  |
| barrier/MSFT/qe | pseudo+antithetic+cv | 1.89x +/- 13% |  |
| autocallable/AAPL/qe | pseudo | 1.00x +/- 13% |  |
| autocallable/AAPL/qe | pseudo+antithetic | 1.52x +/- 13% |  |
| autocallable/AAPL/qe | rqmc | 1.64x +/- 13% |  |
| autocallable/AAPL/qe | rqmc+bridge | 1.84x +/- 13% |  |
| autocallable/AAPL/qe | **INCONCLUSIVE** | every configuration ties: the spread between the best (rqmc+bridge=1.844) and worst (pseudo+antithetic=1.518) configuration (0.326) is smaller than their combined measurement precision (0.427) -- this cell cannot distinguish between configurations | |
| autocallable/MSFT/qe | pseudo | 1.00x +/- 13% |  |
| autocallable/MSFT/qe | pseudo+antithetic | 0.90x +/- 13% |  |
| autocallable/MSFT/qe | rqmc | 1.27x +/- 13% |  |
| autocallable/MSFT/qe | rqmc+bridge | 1.39x +/- 13% | <- BEST |
| vanilla/AAPL/euler-ft | pseudo | 1.00x +/- 13% |  |
| vanilla/AAPL/euler-ft | pseudo+antithetic | 1.43x +/- 13% |  |
| vanilla/AAPL/euler-ft | rqmc | 1.83x +/- 13% |  |
| vanilla/AAPL/euler-ft | rqmc+bridge | 2.56x +/- 13% | <- BEST |

## Bridge contribution

`rmse(config 3) / rmse(config 4)`: isolates the Brownian bridge's own contribution to RQMC's variance reduction (both configurations are RQMC, no control variate).

| cell | ratio |
|---|---|
| vanilla/AAPL/qe | 0.96x +/- 13% |
| vanilla/MSFT/qe | 1.92x +/- 13% |
| barrier/AAPL/qe | 1.31x +/- 13% |
| barrier/MSFT/qe | 1.35x +/- 13% |
| autocallable/AAPL/qe | 1.12x +/- 13% |
| autocallable/MSFT/qe | 1.10x +/- 13% |
| vanilla/AAPL/euler-ft | 1.39x +/- 13% |

## Control-variate contribution

Barrier cells only. `rmse(4)/rmse(5)` isolates the control variate's effect UNDER RQMC; `rmse(2)/rmse(6)` isolates it WITHOUT RQMC (pseudo-random antithetic baseline).

| cell | cv under RQMC (4 vs 5) | cv without RQMC (2 vs 6) |
|---|---|---|
| barrier/AAPL/qe | 1.14x +/- 13% | 1.26x +/- 13% |
| barrier/MSFT/qe | 0.89x +/- 13% | 1.34x +/- 13% |

## SE calibration

`mean_formula_se / rmse` per (cell, config) -- the calibration factor measured on this run's own configuration, replacing the borrowed spike table in `rqmc_estimate`'s docstring. 1.0 means the formula SE is honest; below 1.0 means it is optimistic.

| cell | config | calibration factor |
|---|---|---|
| vanilla/AAPL/qe | pseudo | 0.889 |
| vanilla/AAPL/qe | pseudo+antithetic | 0.930 |
| vanilla/AAPL/qe | rqmc | 1.148 |
| vanilla/AAPL/qe | rqmc+bridge | 1.015 |
| vanilla/MSFT/qe | pseudo | 0.982 |
| vanilla/MSFT/qe | pseudo+antithetic | 1.043 |
| vanilla/MSFT/qe | rqmc | 0.924 |
| vanilla/MSFT/qe | rqmc+bridge | 1.314 |
| barrier/AAPL/qe | pseudo | 0.999 |
| barrier/AAPL/qe | pseudo+antithetic | 1.318 |
| barrier/AAPL/qe | rqmc | 0.835 |
| barrier/AAPL/qe | rqmc+bridge | 1.038 |
| barrier/AAPL/qe | rqmc+bridge+cv | 0.964 |
| barrier/AAPL/qe | pseudo+antithetic+cv | 1.076 |
| barrier/MSFT/qe | pseudo | 1.003 |
| barrier/MSFT/qe | pseudo+antithetic | 1.129 |
| barrier/MSFT/qe | rqmc | 1.049 |
| barrier/MSFT/qe | rqmc+bridge | 1.249 |
| barrier/MSFT/qe | rqmc+bridge+cv | 0.934 |
| barrier/MSFT/qe | pseudo+antithetic+cv | 0.876 |
| autocallable/AAPL/qe | pseudo | 0.878 |
| autocallable/AAPL/qe | pseudo+antithetic | 1.331 |
| autocallable/AAPL/qe | rqmc | 1.012 |
| autocallable/AAPL/qe | rqmc+bridge | 1.119 |
| autocallable/MSFT/qe | pseudo | 1.132 |
| autocallable/MSFT/qe | pseudo+antithetic | 1.011 |
| autocallable/MSFT/qe | rqmc | 1.000 |
| autocallable/MSFT/qe | rqmc+bridge | 0.926 |
| vanilla/AAPL/euler-ft | pseudo | 0.969 |
| vanilla/AAPL/euler-ft | pseudo+antithetic | 1.064 |
| vanilla/AAPL/euler-ft | rqmc | 0.875 |
| vanilla/AAPL/euler-ft | rqmc+bridge | 1.051 |

## Peak memory

`_PEAK_ARRAY_RATIO = 19.0` (tracemalloc-measured, see `exo/tests/test_qmc_memory.py` and this module's own comment on the constant): peak bytes for one `price_rqmc`/`simulate()` call is bounded by `_PEAK_ARRAY_RATIO * n_paths * (n_steps + 1) * 8`. This study refuses to run any (cell, configuration) whose estimated peak exceeds `--max-batch-bytes` rather than batching within a repetition -- at this study's defaults, estimated peak per call stays under 300 MB, far below the 800 MB default ceiling.

## Cell results

Raw per-(cell, config) measurements: `rmse` is the honest empirical spread over `T` repetitions; `mean_formula_se` is the mean of each repetition's own reported standard error.

| cell | config | rmse | mean_formula_se | ratio | repetitions |
|---|---|---|---|---|---|
| vanilla/AAPL/qe | pseudo | 0.11312 | 0.10061 | 1.000 | 32 |
| vanilla/AAPL/qe | pseudo+antithetic | 0.08016 | 0.07456 | 1.411 | 32 |
| vanilla/AAPL/qe | rqmc | 0.03815 | 0.04382 | 2.965 | 32 |
| vanilla/AAPL/qe | rqmc+bridge | 0.03994 | 0.04053 | 2.832 | 32 |
| vanilla/MSFT/qe | pseudo | 0.32165 | 0.31589 | 1.000 | 32 |
| vanilla/MSFT/qe | pseudo+antithetic | 0.20715 | 0.21602 | 1.553 | 32 |
| vanilla/MSFT/qe | rqmc | 0.11190 | 0.10337 | 2.875 | 32 |
| vanilla/MSFT/qe | rqmc+bridge | 0.05818 | 0.07643 | 5.528 | 32 |
| barrier/AAPL/qe | pseudo | 0.09673 | 0.09662 | 1.000 | 32 |
| barrier/AAPL/qe | pseudo+antithetic | 0.05683 | 0.07490 | 1.702 | 32 |
| barrier/AAPL/qe | rqmc | 0.05983 | 0.04995 | 1.617 | 32 |
| barrier/AAPL/qe | rqmc+bridge | 0.04582 | 0.04754 | 2.111 | 32 |
| barrier/AAPL/qe | rqmc+bridge+cv | 0.04019 | 0.03873 | 2.407 | 32 |
| barrier/AAPL/qe | pseudo+antithetic+cv | 0.04509 | 0.04852 | 2.145 | 32 |
| barrier/MSFT/qe | pseudo | 0.30263 | 0.30367 | 1.000 | 32 |
| barrier/MSFT/qe | pseudo+antithetic | 0.21496 | 0.24268 | 1.408 | 32 |
| barrier/MSFT/qe | rqmc | 0.12633 | 0.13246 | 2.396 | 32 |
| barrier/MSFT/qe | rqmc+bridge | 0.09329 | 0.11655 | 3.244 | 32 |
| barrier/MSFT/qe | rqmc+bridge+cv | 0.10497 | 0.09804 | 2.883 | 32 |
| barrier/MSFT/qe | pseudo+antithetic+cv | 0.15991 | 0.14006 | 1.892 | 32 |
_autocallable/AAPL/qe liveness check (product must be genuinely live, not G3b's degenerate identity fixture -- see `_autocallable`'s docstring): observations = [t=0.2, t=0.4, t=0.6, t=0.8, t=1]; redeem-at-obs fraction = [0.609, 0.118, 0.052, 0.031, 0.022]; coupon-at-obs fraction = [0.974, 0.335, 0.207, 0.153, 0.122]; never-redeemed fraction = 0.168_
| autocallable/AAPL/qe | pseudo | 0.59513 | 0.52237 | 1.000 | 32 |
| autocallable/AAPL/qe | pseudo+antithetic | 0.39194 | 0.52165 | 1.518 | 32 |
| autocallable/AAPL/qe | rqmc | 0.36211 | 0.36657 | 1.644 | 32 |
| autocallable/AAPL/qe | rqmc+bridge | 0.32271 | 0.36114 | 1.844 | 32 |
_autocallable/MSFT/qe liveness check (product must be genuinely live, not G3b's degenerate identity fixture -- see `_autocallable`'s docstring): observations = [t=0.2, t=0.4, t=0.6, t=0.8, t=1]; redeem-at-obs fraction = [0.542, 0.125, 0.062, 0.036, 0.025]; coupon-at-obs fraction = [0.975, 0.394, 0.250, 0.177, 0.139]; never-redeemed fraction = 0.211_
| autocallable/MSFT/qe | pseudo | 0.45928 | 0.52003 | 1.000 | 32 |
| autocallable/MSFT/qe | pseudo+antithetic | 0.51146 | 0.51719 | 0.898 | 32 |
| autocallable/MSFT/qe | rqmc | 0.36222 | 0.36229 | 1.268 | 32 |
| autocallable/MSFT/qe | rqmc+bridge | 0.33009 | 0.30557 | 1.391 | 32 |
| vanilla/AAPL/euler-ft | pseudo | 0.10460 | 0.10131 | 1.000 | 32 |
| vanilla/AAPL/euler-ft | pseudo+antithetic | 0.07297 | 0.07763 | 1.433 | 32 |
| vanilla/AAPL/euler-ft | rqmc | 0.05701 | 0.04989 | 1.835 | 32 |
| vanilla/AAPL/euler-ft | rqmc+bridge | 0.04091 | 0.04298 | 2.557 | 32 |
