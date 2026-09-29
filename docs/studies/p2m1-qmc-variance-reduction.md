# P2.M1 Slice 3 -- QMC Variance-Reduction Study

> **Illustrative and UNCALIBRATED parameters (ADR-006 Amendment 4 s1 / Amendment 5).** The Heston parameter sets swept in this study are demo/development values, not calibrated market data, restricted to AAPL and MSFT (Amendment 5) -- SPX's Feller-0.231 regime is P2.M2's warrant-gate trigger and is not swept here.

**Every swept cell found a configuration that measurably beats the pseudo-random denominator.** 5 cell(s) could not resolve a single winner among the top configurations (tied within measurement precision) -- the improvement claim stands regardless; see Cell results.

## Provenance

- base seed: `20260924`
- paths per replicate: `1024`
- repetitions (T): `32`
- git_sha: `841ad351dbfdc90c7fe72e74c3451d874052474d`
- params_hash: `26f5271c72ad778955c05a4582ac644e58459dad5c0cee6961aed1a4622c1261`
- run_id: `2026-09-24T20-22-32Z-qmc-variance-reduction`
- NOTE: `manifest.engine` reflects configuration 4 (RQMC+bridge, QE) only -- the manifest schema has one `[engine]` table; this study sweeps six configurations per cell (see Configurations compared / Cell results below). `repetitions` is recorded here rather than in `manifest.params` (typed `Mapping[str, Mapping[str, float]]` of per-underlying Heston params in `provenance.py`, which this study does not modify).

## Measurement method

Formula SEs from `rqmc_estimate` are optimistic by a factor that grows with both replicate count and net size (heavy-tailed replicate-mean distribution); comparing them directly to pseudo-random formula SEs would inflate every RQMC ratio. Instead each (cell, configuration) runs `T` independent repetitions at the identical total path budget `B`, and `rmse = std(repetition pvs, ddof=1)` -- free of that bias -- is the error measure this study actually ranks on. The headline ratio is `rmse_denominator / rmse_config`; `mean_formula_se / rmse` (SE calibration, a column below) reports the bias actually measured on this run's own configuration. `rank_configs` requires the TOP TWO ratios (not best-vs-worst) to separate by more than their combined measurement precision before naming a single winner; a cell that beats the denominator but cannot separate its top configurations is reported as TOP-TIED, not as a failure.

## Configurations compared

| # | label | sampler | scope |
|---|---|---|---|
| 1 | pseudo | pseudo | all cells |
| 2 | pseudo+antithetic | pseudo | all cells |
| 3 | rqmc | rqmc | all cells |
| 4 | rqmc+bridge | rqmc | all cells |
| 5 | rqmc+bridge+cv | rqmc | barrier cell only |
| 6 | pseudo+antithetic+cv | pseudo | barrier cell only |

## Cell results

Every (cell, config) measured at T=32 repetitions (ratio relative precision +/-13%, `1/sqrt(2*(T-1))`). `ratio` is `rmse_denominator / rmse_config`; `calib` is `mean_formula_se / rmse` (1.0 = honest formula SE, below 1.0 = optimistic). The autocallable is the highest-dimensional, most discontinuous payoff swept here -- RQMC's advantage is EXPECTED to be smaller on that cell than on vanilla, reported as a finding, not adjusted toward a larger number.

| cell | config | ratio | calib | rmse | mean_formula_se | T | outcome |
|---|---|---|---|---|---|---|---|
| vanilla/AAPL/qe | pseudo | 1.00x +/- 13% | 0.889 | 0.11312 | 0.10061 | 32 |  |
| vanilla/AAPL/qe | pseudo+antithetic | 1.41x +/- 13% | 0.930 | 0.08016 | 0.07456 | 32 |  |
| vanilla/AAPL/qe | rqmc | 2.96x +/- 13% | 1.148 | 0.03815 | 0.04382 | 32 | <- TOP-TIED |
| vanilla/AAPL/qe | rqmc+bridge | 2.83x +/- 13% | 1.015 | 0.03994 | 0.04053 | 32 | <- TOP-TIED |
| vanilla/AAPL/qe | **TOP-TIED** | top configurations indistinguishable at T=32: rqmc, rqmc+bridge | | | | | |
| vanilla/MSFT/qe | pseudo | 1.00x +/- 13% | 0.982 | 0.32165 | 0.31589 | 32 |  |
| vanilla/MSFT/qe | pseudo+antithetic | 1.55x +/- 13% | 1.043 | 0.20715 | 0.21602 | 32 |  |
| vanilla/MSFT/qe | rqmc | 2.87x +/- 13% | 0.924 | 0.11190 | 0.10337 | 32 |  |
| vanilla/MSFT/qe | rqmc+bridge | 5.53x +/- 13% | 1.314 | 0.05818 | 0.07643 | 32 | <- BEST |
| barrier/AAPL/qe | pseudo | 1.00x +/- 13% | 0.999 | 0.09673 | 0.09662 | 32 |  |
| barrier/AAPL/qe | pseudo+antithetic | 1.70x +/- 13% | 1.318 | 0.05683 | 0.07490 | 32 | <- TOP-TIED |
| barrier/AAPL/qe | rqmc | 1.62x +/- 13% | 0.835 | 0.05983 | 0.04995 | 32 | <- TOP-TIED |
| barrier/AAPL/qe | rqmc+bridge | 2.11x +/- 13% | 1.038 | 0.04582 | 0.04754 | 32 | <- TOP-TIED |
| barrier/AAPL/qe | rqmc+bridge+cv | 2.42x +/- 13% | 0.969 | 0.03995 | 0.03871 | 32 | <- TOP-TIED |
| barrier/AAPL/qe | pseudo+antithetic+cv | 2.15x +/- 13% | 1.076 | 0.04509 | 0.04852 | 32 | <- TOP-TIED |
| barrier/AAPL/qe | **TOP-TIED** | top configurations indistinguishable at T=32: rqmc+bridge+cv, pseudo+antithetic+cv, rqmc+bridge, pseudo+antithetic, rqmc | | | | | |
| barrier/MSFT/qe | pseudo | 1.00x +/- 13% | 1.003 | 0.30263 | 0.30367 | 32 |  |
| barrier/MSFT/qe | pseudo+antithetic | 1.41x +/- 13% | 1.129 | 0.21496 | 0.24268 | 32 |  |
| barrier/MSFT/qe | rqmc | 2.40x +/- 13% | 1.049 | 0.12633 | 0.13246 | 32 | <- TOP-TIED |
| barrier/MSFT/qe | rqmc+bridge | 3.24x +/- 13% | 1.249 | 0.09329 | 0.11655 | 32 | <- TOP-TIED |
| barrier/MSFT/qe | rqmc+bridge+cv | 2.89x +/- 13% | 0.936 | 0.10472 | 0.09802 | 32 | <- TOP-TIED |
| barrier/MSFT/qe | pseudo+antithetic+cv | 1.89x +/- 13% | 0.876 | 0.15991 | 0.14006 | 32 | <- TOP-TIED |
| barrier/MSFT/qe | **TOP-TIED** | top configurations indistinguishable at T=32: rqmc+bridge, rqmc+bridge+cv, rqmc, pseudo+antithetic+cv | | | | | |
| autocallable/AAPL/qe | pseudo | 1.00x +/- 13% | 0.878 | 0.59513 | 0.52237 | 32 |  |
| autocallable/AAPL/qe | pseudo+antithetic | 1.52x +/- 13% | 1.331 | 0.39194 | 0.52165 | 32 | <- TOP-TIED |
| autocallable/AAPL/qe | rqmc | 1.64x +/- 13% | 1.012 | 0.36211 | 0.36657 | 32 | <- TOP-TIED |
| autocallable/AAPL/qe | rqmc+bridge | 1.84x +/- 13% | 1.119 | 0.32271 | 0.36114 | 32 | <- TOP-TIED |
| autocallable/AAPL/qe | **TOP-TIED** | top configurations indistinguishable at T=32: rqmc+bridge, rqmc, pseudo+antithetic | | | | | |
| autocallable/MSFT/qe | pseudo | 1.00x +/- 13% | 1.132 | 0.45928 | 0.52003 | 32 |  |
| autocallable/MSFT/qe | pseudo+antithetic | 0.90x +/- 13% | 1.011 | 0.51146 | 0.51719 | 32 |  |
| autocallable/MSFT/qe | rqmc | 1.27x +/- 13% | 1.000 | 0.36222 | 0.36229 | 32 | <- TOP-TIED |
| autocallable/MSFT/qe | rqmc+bridge | 1.39x +/- 13% | 0.926 | 0.33009 | 0.30557 | 32 | <- TOP-TIED |
| autocallable/MSFT/qe | **TOP-TIED** | top configurations indistinguishable at T=32: rqmc+bridge, rqmc | | | | | |
| vanilla/AAPL/euler-ft | pseudo | 1.00x +/- 13% | 0.969 | 0.10460 | 0.10131 | 32 |  |
| vanilla/AAPL/euler-ft | pseudo+antithetic | 1.43x +/- 13% | 1.064 | 0.07297 | 0.07763 | 32 |  |
| vanilla/AAPL/euler-ft | rqmc | 1.83x +/- 13% | 0.875 | 0.05701 | 0.04989 | 32 |  |
| vanilla/AAPL/euler-ft | rqmc+bridge | 2.56x +/- 13% | 1.051 | 0.04091 | 0.04298 | 32 | <- BEST |

## Mechanism ratios

Both isolate one mechanism's own contribution, divided from the ratios above: bridge contribution is `rmse(3)/rmse(4)` (both RQMC, no control variate); control-variate contribution (barrier cells only) is `rmse(4)/rmse(5)` under RQMC and `rmse(2)/rmse(6)` without RQMC.

| cell | bridge (3 vs 4) | cv under RQMC (4 vs 5) | cv without RQMC (2 vs 6) |
|---|---|---|---|
| vanilla/AAPL/qe | 0.96x +/- 13% | -- | -- |
| vanilla/MSFT/qe | 1.92x +/- 13% | -- | -- |
| barrier/AAPL/qe | 1.31x +/- 13% | 1.15x +/- 13% | 1.26x +/- 13% |
| barrier/MSFT/qe | 1.35x +/- 13% | 0.89x +/- 13% | 1.34x +/- 13% |
| autocallable/AAPL/qe | 1.12x +/- 13% | -- | -- |
| autocallable/MSFT/qe | 1.10x +/- 13% | -- | -- |
| vanilla/AAPL/euler-ft | 1.39x +/- 13% | -- | -- |

## Peak memory

`_PEAK_ARRAY_RATIO = 19.0` (tracemalloc-measured, see `exo/tests/test_qmc_memory.py` and this module's own comment on the constant): peak bytes for one `price_rqmc`/`simulate()` call is bounded by `_PEAK_ARRAY_RATIO * n_paths * (n_steps + 1) * 8` -- 8 MB at this run's `n_paths`/`n_steps`.
