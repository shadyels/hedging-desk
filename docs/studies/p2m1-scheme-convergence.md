# P2.M1 Slice 1 -- Scheme Convergence Study (QE vs full-truncation Euler)

> **Illustrative and UNCALIBRATED parameters (ADR-006 Amendment 4 s1).** The Heston parameter sets swept in this study are demo/development values, not calibrated market data. The scheme choice recorded below is validated under THESE illustrative parameters; it is not a universal claim and must not be read as one.

**Chosen scheme: `qe`** (fills ADR-006 Amendment 4 s4, previously PENDING).

## Provenance

- base seed: `20260906`
- target paths per cell: `3200000` (actual per-cell total varies slightly by n_steps -- see `n_paths_per_batch`/`n_batches` per row below, module docstring fix 2)
- git_sha: `b813a7654a61c0f401daa47a312401695e1317ba`
- params_hash: `1c6059c947d021bc63613316713a87ad9e965df5b35a05c7d0e06370c9cbb3ee`
- run_id: `2026-09-07T12-22-27Z-scheme-convergence`
- NOTE: `engine.n_steps` in the manifest reflects only the FINEST grid point swept (the manifest schema has one `n_steps` field; this study sweeps seven) -- see the sweep table below for the full grid.

## Ranking rule

The scheme whose `|bias| < 3 * shared_se` first holds for EVERY row (both param sets, every strike/expiry) at a given step count, at the COARSEST such step count, tie-broken on wall time -- where `shared_se` at a cell is the SMALLEST `se` among the schemes present there, so a scheme cannot pass by being noisier than its own peer. A scheme that never passes at any step count is excluded from the ranking; if NO scheme passes at any step count, this run would have raised rather than silently ranking by wall time -- see `rank_schemes`' docstring for the full rule and why an earlier revision's absolute `se_tol` was wrong.

## Cost-to-accuracy comparison

The interesting number is not "which scheme is faster per step" but the RATIO of total wall-clock cost each scheme actually pays to first reach the shared-SE bar above -- a scheme needing far fewer steps can win even when each of its steps costs more.

| scheme | first all-pass n_steps | wall_time_s at that n_steps | cost_per_step_s | cost vs chosen |
|---|---|---|---|---|
| euler-ft | 252 | 119.467 | 0.474075 | 7.57x |
| qe | 12 | 15.783 | 1.315219 | 1.00x |

## Antithetic variance reduction achieved

`se_plain / se_antithetic`, per scheme, at one representative cell (feller_violating, ATM, T=0.5y or the sweep's first expiry). A ratio > 1 is a genuine reduction; a ratio < 1 means antithetics made the estimator WORSE -- a real, reportable possibility under QE (mirroring the variance draw is a valid antithetic but its reduction is not guaranteed), recorded here as a finding, not hidden as a bug.

| scheme | se_plain / se_antithetic |
|---|---|
| qe | 1.3889 |
| euler-ft | 1.3700 |

## Sweep results

Strike rows at a given (scheme, n_steps, param_set, expiry) share one `PathBundle` (common random numbers across strikes -- module docstring fix 1), so their biases are directly comparable rather than differing partly by sampling noise. `qe_fallback_frac` is the fraction of (path, step) cells where QE's martingale correction fell back to the uncorrected K0 (Amendment A1); blank for euler-ft, which has no such fallback.

| scheme | n_steps | param_set | strike | expiry | pv | se | bias | bias/se | wall_time_s | n_paths_per_batch | n_batches | qe_fallback_frac |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| qe | 4 | feller_satisfying | 90.0 | 0.5 | 12.75024 | 0.00287 | +0.00275 | +0.96 | 0.455 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_satisfying | 100.0 | 0.5 | 6.31941 | 0.00350 | +0.00689 | +1.97 | 0.455 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_satisfying | 110.0 | 0.5 | 2.39441 | 0.00280 | +0.00817 | +2.91 | 0.454 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_satisfying | 90.0 | 2.0 | 18.25883 | 0.00767 | -0.00167 | -0.22 | 0.406 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_satisfying | 100.0 | 2.0 | 12.74251 | 0.00793 | -0.00118 | -0.15 | 0.407 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_satisfying | 110.0 | 2.0 | 8.46855 | 0.00748 | -0.00421 | -0.56 | 0.406 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_violating | 90.0 | 0.5 | 12.42752 | 0.00286 | -0.00667 | -2.33 | 0.408 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_violating | 100.0 | 0.5 | 5.21073 | 0.00238 | -0.00274 | -1.15 | 0.408 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_violating | 110.0 | 0.5 | 1.10878 | 0.00164 | +0.01769 | +10.75 | 0.408 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_violating | 90.0 | 2.0 | 16.87887 | 0.00615 | +0.02758 | +4.49 | 0.393 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_violating | 100.0 | 2.0 | 10.48543 | 0.00538 | +0.04156 | +7.73 | 0.402 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_violating | 110.0 | 2.0 | 5.56044 | 0.00456 | +0.00784 | +1.72 | 0.400 | 3200000 | 1 | 0.0000% |
| qe | 12 | feller_satisfying | 90.0 | 0.5 | 12.75018 | 0.00271 | +0.00269 | +0.99 | 1.366 | 1923076 | 2 | 0.0000% |
| qe | 12 | feller_satisfying | 100.0 | 0.5 | 6.31553 | 0.00320 | +0.00300 | +0.94 | 1.365 | 1923076 | 2 | 0.0000% |
| qe | 12 | feller_satisfying | 110.0 | 0.5 | 2.38954 | 0.00255 | +0.00330 | +1.29 | 1.365 | 1923076 | 2 | 0.0000% |
| qe | 12 | feller_satisfying | 90.0 | 2.0 | 18.26388 | 0.00720 | +0.00338 | +0.47 | 1.295 | 1923076 | 2 | 0.0000% |
| qe | 12 | feller_satisfying | 100.0 | 2.0 | 12.74722 | 0.00730 | +0.00354 | +0.48 | 1.290 | 1923076 | 2 | 0.0000% |
| qe | 12 | feller_satisfying | 110.0 | 2.0 | 8.47601 | 0.00684 | +0.00325 | +0.47 | 1.290 | 1923076 | 2 | 0.0000% |
| qe | 12 | feller_violating | 90.0 | 0.5 | 12.43655 | 0.00282 | +0.00236 | +0.84 | 1.303 | 1923076 | 2 | 0.0000% |
| qe | 12 | feller_violating | 100.0 | 0.5 | 5.21522 | 0.00225 | +0.00175 | +0.78 | 1.303 | 1923076 | 2 | 0.0000% |
| qe | 12 | feller_violating | 110.0 | 0.5 | 1.09458 | 0.00150 | +0.00349 | +2.33 | 1.303 | 1923076 | 2 | 0.0000% |
| qe | 12 | feller_violating | 90.0 | 2.0 | 16.85269 | 0.00596 | +0.00139 | +0.23 | 1.300 | 1923076 | 2 | 0.0000% |
| qe | 12 | feller_violating | 100.0 | 2.0 | 10.44523 | 0.00513 | +0.00136 | +0.27 | 1.303 | 1923076 | 2 | 0.0000% |
| qe | 12 | feller_violating | 110.0 | 2.0 | 5.55285 | 0.00424 | +0.00025 | +0.06 | 1.301 | 1923076 | 2 | 0.0000% |
| qe | 52 | feller_satisfying | 90.0 | 0.5 | 12.74819 | 0.00297 | +0.00070 | +0.24 | 6.696 | 471698 | 7 | 0.0000% |
| qe | 52 | feller_satisfying | 100.0 | 0.5 | 6.31287 | 0.00346 | +0.00035 | +0.10 | 6.681 | 471698 | 7 | 0.0000% |
| qe | 52 | feller_satisfying | 110.0 | 0.5 | 2.38577 | 0.00275 | -0.00047 | -0.17 | 6.680 | 471698 | 7 | 0.0000% |
| qe | 52 | feller_satisfying | 90.0 | 2.0 | 18.25884 | 0.00789 | -0.00165 | -0.21 | 6.781 | 471698 | 7 | 0.0000% |
| qe | 52 | feller_satisfying | 100.0 | 2.0 | 12.74125 | 0.00792 | -0.00243 | -0.31 | 6.771 | 471698 | 7 | 0.0000% |
| qe | 52 | feller_satisfying | 110.0 | 2.0 | 8.47059 | 0.00738 | -0.00217 | -0.29 | 6.770 | 471698 | 7 | 0.0000% |
| qe | 52 | feller_violating | 90.0 | 0.5 | 12.43426 | 0.00315 | +0.00006 | +0.02 | 6.743 | 471698 | 7 | 0.0000% |
| qe | 52 | feller_violating | 100.0 | 0.5 | 5.21293 | 0.00248 | -0.00055 | -0.22 | 6.731 | 471698 | 7 | 0.0000% |
| qe | 52 | feller_violating | 110.0 | 0.5 | 1.09131 | 0.00162 | +0.00022 | +0.14 | 6.731 | 471698 | 7 | 0.0000% |
| qe | 52 | feller_violating | 90.0 | 2.0 | 16.84718 | 0.00669 | -0.00411 | -0.61 | 6.672 | 471698 | 7 | 0.0000% |
| qe | 52 | feller_violating | 100.0 | 2.0 | 10.44118 | 0.00570 | -0.00269 | -0.47 | 6.661 | 471698 | 7 | 0.0000% |
| qe | 52 | feller_violating | 110.0 | 2.0 | 5.55248 | 0.00464 | -0.00012 | -0.03 | 6.661 | 471698 | 7 | 0.0000% |
| qe | 104 | feller_satisfying | 90.0 | 0.5 | 12.74697 | 0.00296 | -0.00052 | -0.18 | 13.028 | 238094 | 14 | 0.0000% |
| qe | 104 | feller_satisfying | 100.0 | 0.5 | 6.31281 | 0.00344 | +0.00029 | +0.09 | 12.976 | 238094 | 14 | 0.0000% |
| qe | 104 | feller_satisfying | 110.0 | 0.5 | 2.38782 | 0.00274 | +0.00159 | +0.58 | 12.971 | 238094 | 14 | 0.0000% |
| qe | 104 | feller_satisfying | 90.0 | 2.0 | 18.25987 | 0.00788 | -0.00063 | -0.08 | 13.774 | 238094 | 14 | 0.0000% |
| qe | 104 | feller_satisfying | 100.0 | 2.0 | 12.74441 | 0.00789 | +0.00072 | +0.09 | 13.751 | 238094 | 14 | 0.0000% |
| qe | 104 | feller_satisfying | 110.0 | 2.0 | 8.47385 | 0.00735 | +0.00108 | +0.15 | 13.745 | 238094 | 14 | 0.0000% |
| qe | 104 | feller_violating | 90.0 | 0.5 | 12.43142 | 0.00316 | -0.00278 | -0.88 | 14.223 | 238094 | 14 | 0.0000% |
| qe | 104 | feller_violating | 100.0 | 0.5 | 5.21297 | 0.00248 | -0.00051 | -0.21 | 14.172 | 238094 | 14 | 0.0000% |
| qe | 104 | feller_violating | 110.0 | 0.5 | 1.09252 | 0.00161 | +0.00143 | +0.89 | 14.168 | 238094 | 14 | 0.0000% |
| qe | 104 | feller_violating | 90.0 | 2.0 | 16.84968 | 0.00673 | -0.00161 | -0.24 | 13.735 | 238094 | 14 | 0.0000% |
| qe | 104 | feller_violating | 100.0 | 2.0 | 10.44279 | 0.00573 | -0.00108 | -0.19 | 13.724 | 238094 | 14 | 0.0000% |
| qe | 104 | feller_violating | 110.0 | 2.0 | 5.55323 | 0.00464 | +0.00063 | +0.14 | 13.720 | 238094 | 14 | 0.0000% |
| qe | 252 | feller_satisfying | 90.0 | 0.5 | 12.74945 | 0.00300 | +0.00196 | +0.65 | 29.523 | 98814 | 33 | 0.0000% |
| qe | 252 | feller_satisfying | 100.0 | 0.5 | 6.31561 | 0.00348 | +0.00309 | +0.89 | 29.434 | 98814 | 33 | 0.0000% |
| qe | 252 | feller_satisfying | 110.0 | 0.5 | 2.38895 | 0.00277 | +0.00272 | +0.98 | 29.429 | 98814 | 33 | 0.0000% |
| qe | 252 | feller_satisfying | 90.0 | 2.0 | 18.26586 | 0.00799 | +0.00536 | +0.67 | 31.637 | 98814 | 33 | 0.0000% |
| qe | 252 | feller_satisfying | 100.0 | 2.0 | 12.74976 | 0.00799 | +0.00607 | +0.76 | 31.545 | 98814 | 33 | 0.0000% |
| qe | 252 | feller_satisfying | 110.0 | 2.0 | 8.47927 | 0.00744 | +0.00650 | +0.87 | 31.542 | 98814 | 33 | 0.0000% |
| qe | 252 | feller_violating | 90.0 | 0.5 | 12.43396 | 0.00321 | -0.00023 | -0.07 | 31.549 | 98814 | 33 | 0.0000% |
| qe | 252 | feller_violating | 100.0 | 0.5 | 5.21520 | 0.00251 | +0.00172 | +0.68 | 31.450 | 98814 | 33 | 0.0000% |
| qe | 252 | feller_violating | 110.0 | 0.5 | 1.09279 | 0.00163 | +0.00171 | +1.05 | 31.446 | 98814 | 33 | 0.0000% |
| qe | 252 | feller_violating | 90.0 | 2.0 | 16.85177 | 0.00685 | +0.00048 | +0.07 | 30.032 | 98814 | 33 | 0.0000% |
| qe | 252 | feller_violating | 100.0 | 2.0 | 10.44519 | 0.00582 | +0.00132 | +0.23 | 29.946 | 98814 | 33 | 0.0000% |
| qe | 252 | feller_violating | 110.0 | 2.0 | 5.55430 | 0.00471 | +0.00170 | +0.36 | 29.949 | 98814 | 33 | 0.0000% |
| qe | 504 | feller_satisfying | 90.0 | 0.5 | 12.74860 | 0.00302 | +0.00111 | +0.37 | 50.197 | 49504 | 65 | 0.0000% |
| qe | 504 | feller_satisfying | 100.0 | 0.5 | 6.31208 | 0.00351 | -0.00044 | -0.12 | 49.967 | 49504 | 65 | 0.0000% |
| qe | 504 | feller_satisfying | 110.0 | 0.5 | 2.38745 | 0.00279 | +0.00121 | +0.43 | 49.950 | 49504 | 65 | 0.0000% |
| qe | 504 | feller_satisfying | 90.0 | 2.0 | 18.25974 | 0.00805 | -0.00076 | -0.09 | 49.659 | 49504 | 65 | 0.0000% |
| qe | 504 | feller_satisfying | 100.0 | 2.0 | 12.74206 | 0.00805 | -0.00163 | -0.20 | 49.495 | 49504 | 65 | 0.0000% |
| qe | 504 | feller_satisfying | 110.0 | 2.0 | 8.47313 | 0.00749 | +0.00037 | +0.05 | 49.492 | 49504 | 65 | 0.0000% |
| qe | 504 | feller_violating | 90.0 | 0.5 | 12.43361 | 0.00323 | -0.00059 | -0.18 | 58.980 | 49504 | 65 | 0.0000% |
| qe | 504 | feller_violating | 100.0 | 0.5 | 5.21349 | 0.00253 | +0.00002 | +0.01 | 58.693 | 49504 | 65 | 0.0000% |
| qe | 504 | feller_violating | 110.0 | 0.5 | 1.09073 | 0.00163 | -0.00036 | -0.22 | 58.679 | 49504 | 65 | 0.0000% |
| qe | 504 | feller_violating | 90.0 | 2.0 | 16.85149 | 0.00691 | +0.00020 | +0.03 | 71.353 | 49504 | 65 | 0.0000% |
| qe | 504 | feller_violating | 100.0 | 2.0 | 10.44377 | 0.00587 | -0.00010 | -0.02 | 71.019 | 49504 | 65 | 0.0000% |
| qe | 504 | feller_violating | 110.0 | 2.0 | 5.55219 | 0.00474 | -0.00041 | -0.09 | 71.010 | 49504 | 65 | 0.0000% |
| qe | 1008 | feller_satisfying | 90.0 | 0.5 | 12.74604 | 0.00302 | -0.00146 | -0.48 | 85.216 | 24776 | 130 | 0.0000% |
| qe | 1008 | feller_satisfying | 100.0 | 0.5 | 6.31201 | 0.00350 | -0.00051 | -0.15 | 85.011 | 24776 | 130 | 0.0000% |
| qe | 1008 | feller_satisfying | 110.0 | 0.5 | 2.38568 | 0.00279 | -0.00055 | -0.20 | 84.996 | 24776 | 130 | 0.0000% |
| qe | 1008 | feller_satisfying | 90.0 | 2.0 | 18.25765 | 0.00804 | -0.00285 | -0.35 | 84.620 | 24776 | 130 | 0.0000% |
| qe | 1008 | feller_satisfying | 100.0 | 2.0 | 12.74227 | 0.00804 | -0.00142 | -0.18 | 84.222 | 24776 | 130 | 0.0000% |
| qe | 1008 | feller_satisfying | 110.0 | 2.0 | 8.47228 | 0.00748 | -0.00049 | -0.07 | 84.205 | 24776 | 130 | 0.0000% |
| qe | 1008 | feller_violating | 90.0 | 0.5 | 12.42933 | 0.00323 | -0.00487 | -1.51 | 83.702 | 24776 | 130 | 0.0000% |
| qe | 1008 | feller_violating | 100.0 | 0.5 | 5.21043 | 0.00253 | -0.00305 | -1.21 | 83.309 | 24776 | 130 | 0.0000% |
| qe | 1008 | feller_violating | 110.0 | 0.5 | 1.08881 | 0.00163 | -0.00228 | -1.40 | 83.294 | 24776 | 130 | 0.0000% |
| qe | 1008 | feller_violating | 90.0 | 2.0 | 16.84337 | 0.00691 | -0.00792 | -1.15 | 86.570 | 24776 | 130 | 0.0000% |
| qe | 1008 | feller_violating | 100.0 | 2.0 | 10.43791 | 0.00587 | -0.00596 | -1.02 | 86.046 | 24776 | 130 | 0.0000% |
| qe | 1008 | feller_violating | 110.0 | 2.0 | 5.54826 | 0.00474 | -0.00434 | -0.92 | 86.031 | 24776 | 130 | 0.0000% |
| euler-ft | 4 | feller_satisfying | 90.0 | 0.5 | 12.75680 | 0.00348 | +0.00931 | +2.68 | 0.115 | 3200000 | 1 |  |
| euler-ft | 4 | feller_satisfying | 100.0 | 0.5 | 6.37878 | 0.00375 | +0.06626 | +17.69 | 0.115 | 3200000 | 1 |  |
| euler-ft | 4 | feller_satisfying | 110.0 | 0.5 | 2.47612 | 0.00292 | +0.08989 | +30.78 | 0.115 | 3200000 | 1 |  |
| euler-ft | 4 | feller_satisfying | 90.0 | 2.0 | 18.44522 | 0.00952 | +0.18472 | +19.40 | 0.112 | 3200000 | 1 |  |
| euler-ft | 4 | feller_satisfying | 100.0 | 2.0 | 12.96973 | 0.00903 | +0.22604 | +25.02 | 0.112 | 3200000 | 1 |  |
| euler-ft | 4 | feller_satisfying | 110.0 | 2.0 | 8.72718 | 0.00815 | +0.25442 | +31.22 | 0.111 | 3200000 | 1 |  |
| euler-ft | 4 | feller_violating | 90.0 | 0.5 | 12.58610 | 0.00374 | +0.15190 | +40.63 | 0.118 | 3200000 | 1 |  |
| euler-ft | 4 | feller_violating | 100.0 | 0.5 | 5.65515 | 0.00317 | +0.44168 | +139.29 | 0.118 | 3200000 | 1 |  |
| euler-ft | 4 | feller_violating | 110.0 | 0.5 | 1.55174 | 0.00209 | +0.46065 | +220.44 | 0.117 | 3200000 | 1 |  |
| euler-ft | 4 | feller_violating | 90.0 | 2.0 | 17.85242 | 0.00850 | +1.00113 | +117.76 | 0.112 | 3200000 | 1 |  |
| euler-ft | 4 | feller_violating | 100.0 | 2.0 | 11.71848 | 0.00766 | +1.27461 | +166.44 | 0.111 | 3200000 | 1 |  |
| euler-ft | 4 | feller_violating | 110.0 | 2.0 | 6.98184 | 0.00658 | +1.42923 | +217.34 | 0.111 | 3200000 | 1 |  |
| euler-ft | 12 | feller_satisfying | 90.0 | 0.5 | 12.75016 | 0.00289 | +0.00267 | +0.92 | 0.520 | 1923076 | 2 |  |
| euler-ft | 12 | feller_satisfying | 100.0 | 0.5 | 6.32933 | 0.00326 | +0.01681 | +5.16 | 0.521 | 1923076 | 2 |  |
| euler-ft | 12 | feller_satisfying | 110.0 | 0.5 | 2.40908 | 0.00258 | +0.02285 | +8.86 | 0.519 | 1923076 | 2 |  |
| euler-ft | 12 | feller_satisfying | 90.0 | 2.0 | 18.28982 | 0.00787 | +0.02932 | +3.73 | 0.471 | 1923076 | 2 |  |
| euler-ft | 12 | feller_satisfying | 100.0 | 2.0 | 12.77516 | 0.00762 | +0.03147 | +4.13 | 0.471 | 1923076 | 2 |  |
| euler-ft | 12 | feller_satisfying | 110.0 | 2.0 | 8.50629 | 0.00696 | +0.03353 | +4.82 | 0.471 | 1923076 | 2 |  |
| euler-ft | 12 | feller_violating | 90.0 | 0.5 | 12.48741 | 0.00317 | +0.05322 | +16.79 | 0.469 | 1923076 | 2 |  |
| euler-ft | 12 | feller_violating | 100.0 | 0.5 | 5.32917 | 0.00252 | +0.11569 | +45.88 | 0.470 | 1923076 | 2 |  |
| euler-ft | 12 | feller_violating | 110.0 | 0.5 | 1.19198 | 0.00159 | +0.10089 | +63.42 | 0.469 | 1923076 | 2 |  |
| euler-ft | 12 | feller_violating | 90.0 | 2.0 | 17.12043 | 0.00678 | +0.26914 | +39.67 | 0.474 | 1923076 | 2 |  |
| euler-ft | 12 | feller_violating | 100.0 | 2.0 | 10.78523 | 0.00585 | +0.34136 | +58.40 | 0.474 | 1923076 | 2 |  |
| euler-ft | 12 | feller_violating | 110.0 | 2.0 | 5.93189 | 0.00477 | +0.37929 | +79.49 | 0.474 | 1923076 | 2 |  |
| euler-ft | 52 | feller_satisfying | 90.0 | 0.5 | 12.74386 | 0.00301 | -0.00364 | -1.21 | 2.797 | 471698 | 7 |  |
| euler-ft | 52 | feller_satisfying | 100.0 | 0.5 | 6.31368 | 0.00347 | +0.00116 | +0.33 | 2.766 | 471698 | 7 |  |
| euler-ft | 52 | feller_satisfying | 110.0 | 0.5 | 2.38974 | 0.00276 | +0.00351 | +1.27 | 2.767 | 471698 | 7 |  |
| euler-ft | 52 | feller_satisfying | 90.0 | 2.0 | 18.25605 | 0.00806 | -0.00445 | -0.55 | 2.607 | 471698 | 7 |  |
| euler-ft | 52 | feller_satisfying | 100.0 | 2.0 | 12.74139 | 0.00798 | -0.00230 | -0.29 | 2.604 | 471698 | 7 |  |
| euler-ft | 52 | feller_satisfying | 110.0 | 2.0 | 8.47237 | 0.00740 | -0.00039 | -0.05 | 2.604 | 471698 | 7 |  |
| euler-ft | 52 | feller_violating | 90.0 | 0.5 | 12.43834 | 0.00327 | +0.00415 | +1.27 | 2.602 | 471698 | 7 |  |
| euler-ft | 52 | feller_violating | 100.0 | 0.5 | 5.22833 | 0.00256 | +0.01485 | +5.80 | 2.604 | 471698 | 7 |  |
| euler-ft | 52 | feller_violating | 110.0 | 0.5 | 1.10548 | 0.00163 | +0.01440 | +8.83 | 2.602 | 471698 | 7 |  |
| euler-ft | 52 | feller_violating | 90.0 | 2.0 | 16.88293 | 0.00701 | +0.03164 | +4.52 | 2.617 | 471698 | 7 |  |
| euler-ft | 52 | feller_violating | 100.0 | 2.0 | 10.49043 | 0.00595 | +0.04657 | +7.82 | 2.616 | 471698 | 7 |  |
| euler-ft | 52 | feller_violating | 110.0 | 2.0 | 5.60830 | 0.00479 | +0.05569 | +11.63 | 2.615 | 471698 | 7 |  |
| euler-ft | 104 | feller_satisfying | 90.0 | 0.5 | 12.74432 | 0.00298 | -0.00317 | -1.06 | 4.850 | 238094 | 14 |  |
| euler-ft | 104 | feller_satisfying | 100.0 | 0.5 | 6.30982 | 0.00344 | -0.00270 | -0.78 | 4.837 | 238094 | 14 |  |
| euler-ft | 104 | feller_satisfying | 110.0 | 0.5 | 2.38547 | 0.00274 | -0.00076 | -0.28 | 4.834 | 238094 | 14 |  |
| euler-ft | 104 | feller_satisfying | 90.0 | 2.0 | 18.25582 | 0.00796 | -0.00468 | -0.59 | 4.806 | 238094 | 14 |  |
| euler-ft | 104 | feller_satisfying | 100.0 | 2.0 | 12.73888 | 0.00792 | -0.00481 | -0.61 | 4.799 | 238094 | 14 |  |
| euler-ft | 104 | feller_satisfying | 110.0 | 2.0 | 8.46908 | 0.00735 | -0.00368 | -0.50 | 4.795 | 238094 | 14 |  |
| euler-ft | 104 | feller_violating | 90.0 | 0.5 | 12.43275 | 0.00322 | -0.00144 | -0.45 | 4.862 | 238094 | 14 |  |
| euler-ft | 104 | feller_violating | 100.0 | 0.5 | 5.21503 | 0.00252 | +0.00155 | +0.62 | 4.847 | 238094 | 14 |  |
| euler-ft | 104 | feller_violating | 110.0 | 0.5 | 1.09378 | 0.00161 | +0.00270 | +1.67 | 4.844 | 238094 | 14 |  |
| euler-ft | 104 | feller_violating | 90.0 | 2.0 | 16.85764 | 0.00691 | +0.00634 | +0.92 | 4.810 | 238094 | 14 |  |
| euler-ft | 104 | feller_violating | 100.0 | 2.0 | 10.45663 | 0.00586 | +0.01276 | +2.18 | 4.807 | 238094 | 14 |  |
| euler-ft | 104 | feller_violating | 110.0 | 2.0 | 5.57002 | 0.00471 | +0.01742 | +3.70 | 4.804 | 238094 | 14 |  |
| euler-ft | 252 | feller_satisfying | 90.0 | 0.5 | 12.74558 | 0.00301 | -0.00191 | -0.63 | 9.767 | 98814 | 33 |  |
| euler-ft | 252 | feller_satisfying | 100.0 | 0.5 | 6.31059 | 0.00349 | -0.00193 | -0.55 | 9.758 | 98814 | 33 |  |
| euler-ft | 252 | feller_satisfying | 110.0 | 0.5 | 2.38684 | 0.00277 | +0.00060 | +0.22 | 9.751 | 98814 | 33 |  |
| euler-ft | 252 | feller_satisfying | 90.0 | 2.0 | 18.25439 | 0.00803 | -0.00611 | -0.76 | 10.351 | 98814 | 33 |  |
| euler-ft | 252 | feller_satisfying | 100.0 | 2.0 | 12.73904 | 0.00800 | -0.00465 | -0.58 | 10.268 | 98814 | 33 |  |
| euler-ft | 252 | feller_satisfying | 110.0 | 2.0 | 8.47056 | 0.00745 | -0.00220 | -0.30 | 10.260 | 98814 | 33 |  |
| euler-ft | 252 | feller_violating | 90.0 | 0.5 | 12.42943 | 0.00324 | -0.00476 | -1.47 | 10.008 | 98814 | 33 |  |
| euler-ft | 252 | feller_violating | 100.0 | 0.5 | 5.21244 | 0.00254 | -0.00103 | -0.41 | 9.978 | 98814 | 33 |  |
| euler-ft | 252 | feller_violating | 110.0 | 0.5 | 1.09280 | 0.00163 | +0.00171 | +1.05 | 9.974 | 98814 | 33 |  |
| euler-ft | 252 | feller_violating | 90.0 | 2.0 | 16.84289 | 0.00694 | -0.00840 | -1.21 | 9.817 | 98814 | 33 |  |
| euler-ft | 252 | feller_violating | 100.0 | 2.0 | 10.44003 | 0.00589 | -0.00384 | -0.65 | 9.771 | 98814 | 33 |  |
| euler-ft | 252 | feller_violating | 110.0 | 2.0 | 5.55437 | 0.00475 | +0.00176 | +0.37 | 9.764 | 98814 | 33 |  |
| euler-ft | 504 | feller_satisfying | 90.0 | 0.5 | 12.74462 | 0.00303 | -0.00287 | -0.95 | 15.886 | 49504 | 65 |  |
| euler-ft | 504 | feller_satisfying | 100.0 | 0.5 | 6.31118 | 0.00351 | -0.00134 | -0.38 | 15.865 | 49504 | 65 |  |
| euler-ft | 504 | feller_satisfying | 110.0 | 0.5 | 2.38534 | 0.00279 | -0.00090 | -0.32 | 15.858 | 49504 | 65 |  |
| euler-ft | 504 | feller_satisfying | 90.0 | 2.0 | 18.25656 | 0.00807 | -0.00394 | -0.49 | 15.873 | 49504 | 65 |  |
| euler-ft | 504 | feller_satisfying | 100.0 | 2.0 | 12.74069 | 0.00805 | -0.00300 | -0.37 | 15.863 | 49504 | 65 |  |
| euler-ft | 504 | feller_satisfying | 110.0 | 2.0 | 8.46998 | 0.00749 | -0.00278 | -0.37 | 15.860 | 49504 | 65 |  |
| euler-ft | 504 | feller_violating | 90.0 | 0.5 | 12.43068 | 0.00325 | -0.00352 | -1.08 | 16.004 | 49504 | 65 |  |
| euler-ft | 504 | feller_violating | 100.0 | 0.5 | 5.21282 | 0.00255 | -0.00066 | -0.26 | 15.893 | 49504 | 65 |  |
| euler-ft | 504 | feller_violating | 110.0 | 0.5 | 1.09157 | 0.00164 | +0.00048 | +0.29 | 15.887 | 49504 | 65 |  |
| euler-ft | 504 | feller_violating | 90.0 | 2.0 | 16.84388 | 0.00698 | -0.00741 | -1.06 | 15.937 | 49504 | 65 |  |
| euler-ft | 504 | feller_violating | 100.0 | 2.0 | 10.43994 | 0.00592 | -0.00393 | -0.66 | 15.904 | 49504 | 65 |  |
| euler-ft | 504 | feller_violating | 110.0 | 2.0 | 5.55220 | 0.00477 | -0.00041 | -0.09 | 15.900 | 49504 | 65 |  |
| euler-ft | 1008 | feller_satisfying | 90.0 | 0.5 | 12.74653 | 0.00303 | -0.00096 | -0.32 | 31.774 | 24776 | 130 |  |
| euler-ft | 1008 | feller_satisfying | 100.0 | 0.5 | 6.31536 | 0.00350 | +0.00283 | +0.81 | 31.433 | 24776 | 130 |  |
| euler-ft | 1008 | feller_satisfying | 110.0 | 0.5 | 2.38794 | 0.00279 | +0.00170 | +0.61 | 31.433 | 24776 | 130 |  |
| euler-ft | 1008 | feller_satisfying | 90.0 | 2.0 | 18.26208 | 0.00806 | +0.00158 | +0.20 | 31.483 | 24776 | 130 |  |
| euler-ft | 1008 | feller_satisfying | 100.0 | 2.0 | 12.74882 | 0.00804 | +0.00513 | +0.64 | 31.368 | 24776 | 130 |  |
| euler-ft | 1008 | feller_satisfying | 110.0 | 2.0 | 8.47691 | 0.00749 | +0.00415 | +0.55 | 31.370 | 24776 | 130 |  |
| euler-ft | 1008 | feller_violating | 90.0 | 0.5 | 12.42975 | 0.00324 | -0.00445 | -1.37 | 31.052 | 24776 | 130 |  |
| euler-ft | 1008 | feller_violating | 100.0 | 0.5 | 5.21428 | 0.00254 | +0.00080 | +0.32 | 30.469 | 24776 | 130 |  |
| euler-ft | 1008 | feller_violating | 110.0 | 0.5 | 1.09109 | 0.00164 | -0.00000 | -0.00 | 30.468 | 24776 | 130 |  |
| euler-ft | 1008 | feller_violating | 90.0 | 2.0 | 16.84748 | 0.00696 | -0.00381 | -0.55 | 31.126 | 24776 | 130 |  |
| euler-ft | 1008 | feller_violating | 100.0 | 2.0 | 10.44549 | 0.00590 | +0.00162 | +0.27 | 30.805 | 24776 | 130 |  |
| euler-ft | 1008 | feller_violating | 110.0 | 2.0 | 5.55490 | 0.00475 | +0.00230 | +0.48 | 30.807 | 24776 | 130 |  |
