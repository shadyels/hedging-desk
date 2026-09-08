# P2.M1 Slice 1 -- Scheme Convergence Study (QE vs full-truncation Euler)

> **Illustrative and UNCALIBRATED parameters (ADR-006 Amendment 4 s1).** The Heston parameter sets swept in this study are demo/development values, not calibrated market data. The scheme choice recorded below is validated under THESE illustrative parameters; it is not a universal claim and must not be read as one.

**Chosen scheme: `qe`** (fills ADR-006 Amendment 4 s4, previously PENDING).

## Provenance

- base seed: `20260906`
- target paths per cell: `3200000` (actual per-cell total varies slightly by n_steps -- see `n_paths_per_batch`/`n_batches` per row below, module docstring fix 2)
- git_sha: `0a36eba54ff9cbbe0410ecbf2a11b23d4e83e1c5`
- params_hash: `1c6059c947d021bc63613316713a87ad9e965df5b35a05c7d0e06370c9cbb3ee`
- run_id: `2026-09-07T21-10-10Z-scheme-convergence`
- NOTE: `engine.n_steps` in the manifest reflects only the FINEST grid point swept (the manifest schema has one `n_steps` field; this study sweeps seven) -- see the sweep table below for the full grid.

## Ranking rule

The scheme whose `|bias| < 3 * shared_se` first holds for EVERY row (both param sets, every strike/expiry) at a given step count, at the COARSEST such step count, tie-broken on wall time -- where `shared_se` at a cell is the SMALLEST `se` among the schemes present there, so a scheme cannot pass by being noisier than its own peer. A scheme that never passes at any step count is excluded from the ranking; if NO scheme passes at any step count, this run would have raised rather than silently ranking by wall time -- see `rank_schemes`' docstring for the full rule and why an earlier revision's absolute `se_tol` was wrong.

## Cost-to-accuracy comparison

The interesting number is not "which scheme is faster per step" but the RATIO of total wall-clock cost each scheme actually pays to first reach the shared-SE bar above -- a scheme needing far fewer steps can win even when each of its steps costs more. `total_paths` is the actual path count simulated at that scheme's first-passing `n_steps` (SHOULD-9, third code-review round, 2026-09-06): compared schemes can simulate UNEQUAL totals here, since `resolve_batch_plan`'s `ceil` rounding is a function of `n_steps` -- when the scheme with the FEWER total paths still wins (a larger `total_paths` inflates that scheme's own wall time, working against it), the comparison is CONSERVATIVE, not favorable to the winner.

| scheme | first all-pass n_steps | wall_time_s at that n_steps | total_paths | cost_per_step_s | cost vs chosen |
|---|---|---|---|---|---|
| euler-ft | 104 | 50.581 | 3238092 | 0.486360 | 2.65x |
| qe | 12 | 19.066 | 4615380 | 1.588796 | 1.00x |

## Antithetic variance reduction achieved

`se_plain / se_antithetic`, per scheme, at one representative cell (feller_violating, ATM, T=0.5y or the sweep's first expiry, n_steps=`104`, n_paths=`190476` -- named explicitly, SHOULD-9 third code-review round 2026-09-06, so these two ratios are reproducible from the artifact alone). A ratio > 1 is a genuine reduction; a ratio < 1 means antithetics made the estimator WORSE -- a real, reportable possibility under QE (mirroring the variance draw is a valid antithetic but its reduction is not guaranteed), recorded here as a finding, not hidden as a bug.

| scheme | se_plain / se_antithetic |
|---|---|
| qe | 1.3937 |
| euler-ft | 1.3715 |

## Sweep results

Strike rows at a given (scheme, n_steps, param_set, expiry) share one `PathBundle` (common random numbers across strikes -- module docstring fix 1), so their biases are directly comparable rather than differing partly by sampling noise. `qe_fallback_frac` is the fraction of (path, step) cells where QE's martingale correction fell back to the uncorrected K0 (Amendment A1); blank for euler-ft, which has no such fallback.

| scheme | n_steps | param_set | strike | expiry | pv | se | bias | bias/se | wall_time_s | n_paths_per_batch | n_batches | qe_fallback_frac |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| qe | 4 | feller_satisfying | 90.0 | 0.5 | 12.75024 | 0.00287 | +0.00275 | +0.96 | 0.485 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_satisfying | 100.0 | 0.5 | 6.31941 | 0.00350 | +0.00689 | +1.97 | 0.485 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_satisfying | 110.0 | 0.5 | 2.39441 | 0.00280 | +0.00817 | +2.91 | 0.485 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_satisfying | 90.0 | 2.0 | 18.25883 | 0.00767 | -0.00167 | -0.22 | 0.407 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_satisfying | 100.0 | 2.0 | 12.74251 | 0.00793 | -0.00118 | -0.15 | 0.407 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_satisfying | 110.0 | 2.0 | 8.46855 | 0.00748 | -0.00421 | -0.56 | 0.407 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_violating | 90.0 | 0.5 | 12.42752 | 0.00286 | -0.00667 | -2.33 | 0.407 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_violating | 100.0 | 0.5 | 5.21073 | 0.00238 | -0.00274 | -1.15 | 0.408 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_violating | 110.0 | 0.5 | 1.10878 | 0.00164 | +0.01769 | +10.75 | 0.408 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_violating | 90.0 | 2.0 | 16.87887 | 0.00615 | +0.02758 | +4.49 | 0.389 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_violating | 100.0 | 2.0 | 10.48543 | 0.00538 | +0.04156 | +7.73 | 0.390 | 3200000 | 1 | 0.0000% |
| qe | 4 | feller_violating | 110.0 | 2.0 | 5.56044 | 0.00456 | +0.00784 | +1.72 | 0.389 | 3200000 | 1 | 0.0000% |
| qe | 12 | feller_satisfying | 90.0 | 0.5 | 12.75035 | 0.00247 | +0.00286 | +1.15 | 1.608 | 1538460 | 3 | 0.0000% |
| qe | 12 | feller_satisfying | 100.0 | 0.5 | 6.31601 | 0.00292 | +0.00349 | +1.20 | 1.608 | 1538460 | 3 | 0.0000% |
| qe | 12 | feller_satisfying | 110.0 | 0.5 | 2.39000 | 0.00233 | +0.00376 | +1.61 | 1.607 | 1538460 | 3 | 0.0000% |
| qe | 12 | feller_satisfying | 90.0 | 2.0 | 18.26410 | 0.00657 | +0.00360 | +0.55 | 1.541 | 1538460 | 3 | 0.0000% |
| qe | 12 | feller_satisfying | 100.0 | 2.0 | 12.74839 | 0.00667 | +0.00470 | +0.70 | 1.536 | 1538460 | 3 | 0.0000% |
| qe | 12 | feller_satisfying | 110.0 | 2.0 | 8.47783 | 0.00625 | +0.00506 | +0.81 | 1.542 | 1538460 | 3 | 0.0000% |
| qe | 12 | feller_violating | 90.0 | 0.5 | 12.43591 | 0.00257 | +0.00172 | +0.67 | 1.608 | 1538460 | 3 | 0.0000% |
| qe | 12 | feller_violating | 100.0 | 0.5 | 5.21460 | 0.00205 | +0.00112 | +0.55 | 1.607 | 1538460 | 3 | 0.0000% |
| qe | 12 | feller_violating | 110.0 | 0.5 | 1.09421 | 0.00137 | +0.00312 | +2.28 | 1.607 | 1538460 | 3 | 0.0000% |
| qe | 12 | feller_violating | 90.0 | 2.0 | 16.84861 | 0.00544 | -0.00268 | -0.49 | 1.604 | 1538460 | 3 | 0.0000% |
| qe | 12 | feller_violating | 100.0 | 2.0 | 10.44212 | 0.00468 | -0.00175 | -0.37 | 1.599 | 1538460 | 3 | 0.0000% |
| qe | 12 | feller_violating | 110.0 | 2.0 | 5.55232 | 0.00387 | -0.00029 | -0.07 | 1.599 | 1538460 | 3 | 0.0000% |
| qe | 52 | feller_satisfying | 90.0 | 0.5 | 12.74671 | 0.00293 | -0.00078 | -0.27 | 7.040 | 377358 | 9 | 0.0000% |
| qe | 52 | feller_satisfying | 100.0 | 0.5 | 6.31131 | 0.00341 | -0.00121 | -0.36 | 7.026 | 377358 | 9 | 0.0000% |
| qe | 52 | feller_satisfying | 110.0 | 0.5 | 2.38394 | 0.00271 | -0.00230 | -0.85 | 7.025 | 377358 | 9 | 0.0000% |
| qe | 52 | feller_satisfying | 90.0 | 2.0 | 18.25358 | 0.00778 | -0.00692 | -0.89 | 7.301 | 377358 | 9 | 0.0000% |
| qe | 52 | feller_satisfying | 100.0 | 2.0 | 12.73590 | 0.00780 | -0.00779 | -1.00 | 7.287 | 377358 | 9 | 0.0000% |
| qe | 52 | feller_satisfying | 110.0 | 2.0 | 8.46523 | 0.00728 | -0.00753 | -1.04 | 7.284 | 377358 | 9 | 0.0000% |
| qe | 52 | feller_violating | 90.0 | 0.5 | 12.43414 | 0.00311 | -0.00005 | -0.02 | 7.319 | 377358 | 9 | 0.0000% |
| qe | 52 | feller_violating | 100.0 | 0.5 | 5.21245 | 0.00244 | -0.00102 | -0.42 | 7.300 | 377358 | 9 | 0.0000% |
| qe | 52 | feller_violating | 110.0 | 0.5 | 1.09089 | 0.00159 | -0.00020 | -0.12 | 7.298 | 377358 | 9 | 0.0000% |
| qe | 52 | feller_violating | 90.0 | 2.0 | 16.84384 | 0.00660 | -0.00745 | -1.13 | 7.744 | 377358 | 9 | 0.0000% |
| qe | 52 | feller_violating | 100.0 | 2.0 | 10.43791 | 0.00562 | -0.00596 | -1.06 | 7.739 | 377358 | 9 | 0.0000% |
| qe | 52 | feller_violating | 110.0 | 2.0 | 5.54944 | 0.00457 | -0.00317 | -0.69 | 7.737 | 377358 | 9 | 0.0000% |
| qe | 104 | feller_satisfying | 90.0 | 0.5 | 12.74555 | 0.00301 | -0.00194 | -0.64 | 12.715 | 190476 | 17 | 0.0000% |
| qe | 104 | feller_satisfying | 100.0 | 0.5 | 6.31222 | 0.00349 | -0.00030 | -0.09 | 12.671 | 190476 | 17 | 0.0000% |
| qe | 104 | feller_satisfying | 110.0 | 0.5 | 2.38757 | 0.00278 | +0.00133 | +0.48 | 12.671 | 190476 | 17 | 0.0000% |
| qe | 104 | feller_satisfying | 90.0 | 2.0 | 18.25482 | 0.00800 | -0.00568 | -0.71 | 12.540 | 190476 | 17 | 0.0000% |
| qe | 104 | feller_satisfying | 100.0 | 2.0 | 12.74042 | 0.00801 | -0.00326 | -0.41 | 12.484 | 190476 | 17 | 0.0000% |
| qe | 104 | feller_satisfying | 110.0 | 2.0 | 8.47218 | 0.00746 | -0.00058 | -0.08 | 12.479 | 190476 | 17 | 0.0000% |
| qe | 104 | feller_violating | 90.0 | 0.5 | 12.42814 | 0.00320 | -0.00605 | -1.89 | 13.149 | 190476 | 17 | 0.0000% |
| qe | 104 | feller_violating | 100.0 | 0.5 | 5.21100 | 0.00252 | -0.00248 | -0.98 | 13.126 | 190476 | 17 | 0.0000% |
| qe | 104 | feller_violating | 110.0 | 0.5 | 1.09216 | 0.00163 | +0.00107 | +0.66 | 13.125 | 190476 | 17 | 0.0000% |
| qe | 104 | feller_violating | 90.0 | 2.0 | 16.84066 | 0.00682 | -0.01063 | -1.56 | 12.893 | 190476 | 17 | 0.0000% |
| qe | 104 | feller_violating | 100.0 | 2.0 | 10.43593 | 0.00581 | -0.00793 | -1.37 | 12.866 | 190476 | 17 | 0.0000% |
| qe | 104 | feller_violating | 110.0 | 2.0 | 5.54951 | 0.00471 | -0.00309 | -0.66 | 12.866 | 190476 | 17 | 0.0000% |
| qe | 252 | feller_satisfying | 90.0 | 0.5 | 12.74568 | 0.00301 | -0.00182 | -0.60 | 27.551 | 79050 | 41 | 0.0000% |
| qe | 252 | feller_satisfying | 100.0 | 0.5 | 6.31187 | 0.00349 | -0.00065 | -0.19 | 27.454 | 79050 | 41 | 0.0000% |
| qe | 252 | feller_satisfying | 110.0 | 0.5 | 2.38602 | 0.00278 | -0.00022 | -0.08 | 27.452 | 79050 | 41 | 0.0000% |
| qe | 252 | feller_satisfying | 90.0 | 2.0 | 18.25709 | 0.00802 | -0.00340 | -0.42 | 28.131 | 79050 | 41 | 0.0000% |
| qe | 252 | feller_satisfying | 100.0 | 2.0 | 12.74127 | 0.00801 | -0.00242 | -0.30 | 28.052 | 79050 | 41 | 0.0000% |
| qe | 252 | feller_satisfying | 110.0 | 2.0 | 8.47202 | 0.00746 | -0.00074 | -0.10 | 28.051 | 79050 | 41 | 0.0000% |
| qe | 252 | feller_violating | 90.0 | 0.5 | 12.43102 | 0.00322 | -0.00318 | -0.99 | 27.503 | 79050 | 41 | 0.0000% |
| qe | 252 | feller_violating | 100.0 | 0.5 | 5.21210 | 0.00252 | -0.00138 | -0.55 | 27.400 | 79050 | 41 | 0.0000% |
| qe | 252 | feller_violating | 110.0 | 0.5 | 1.09112 | 0.00163 | +0.00003 | +0.02 | 27.398 | 79050 | 41 | 0.0000% |
| qe | 252 | feller_violating | 90.0 | 2.0 | 16.84606 | 0.00687 | -0.00524 | -0.76 | 27.445 | 79050 | 41 | 0.0000% |
| qe | 252 | feller_violating | 100.0 | 2.0 | 10.43931 | 0.00584 | -0.00456 | -0.78 | 27.358 | 79050 | 41 | 0.0000% |
| qe | 252 | feller_violating | 110.0 | 2.0 | 5.54957 | 0.00472 | -0.00303 | -0.64 | 27.357 | 79050 | 41 | 0.0000% |
| qe | 504 | feller_satisfying | 90.0 | 0.5 | 12.74660 | 0.00303 | -0.00089 | -0.29 | 44.330 | 39602 | 81 | 0.0000% |
| qe | 504 | feller_satisfying | 100.0 | 0.5 | 6.31165 | 0.00351 | -0.00087 | -0.25 | 44.076 | 39602 | 81 | 0.0000% |
| qe | 504 | feller_satisfying | 110.0 | 0.5 | 2.38742 | 0.00279 | +0.00119 | +0.42 | 44.062 | 39602 | 81 | 0.0000% |
| qe | 504 | feller_satisfying | 90.0 | 2.0 | 18.25593 | 0.00806 | -0.00457 | -0.57 | 45.302 | 39602 | 81 | 0.0000% |
| qe | 504 | feller_satisfying | 100.0 | 2.0 | 12.73964 | 0.00806 | -0.00405 | -0.50 | 45.096 | 39602 | 81 | 0.0000% |
| qe | 504 | feller_satisfying | 110.0 | 2.0 | 8.47150 | 0.00750 | -0.00126 | -0.17 | 45.081 | 39602 | 81 | 0.0000% |
| qe | 504 | feller_violating | 90.0 | 0.5 | 12.42918 | 0.00323 | -0.00501 | -1.55 | 44.032 | 39602 | 81 | 0.0000% |
| qe | 504 | feller_violating | 100.0 | 0.5 | 5.21131 | 0.00253 | -0.00216 | -0.85 | 43.785 | 39602 | 81 | 0.0000% |
| qe | 504 | feller_violating | 110.0 | 0.5 | 1.09069 | 0.00164 | -0.00040 | -0.24 | 43.772 | 39602 | 81 | 0.0000% |
| qe | 504 | feller_violating | 90.0 | 2.0 | 16.83973 | 0.00692 | -0.01156 | -1.67 | 45.246 | 39602 | 81 | 0.0000% |
| qe | 504 | feller_violating | 100.0 | 2.0 | 10.43538 | 0.00588 | -0.00849 | -1.44 | 44.910 | 39602 | 81 | 0.0000% |
| qe | 504 | feller_violating | 110.0 | 2.0 | 5.54786 | 0.00475 | -0.00474 | -1.00 | 44.895 | 39602 | 81 | 0.0000% |
| qe | 1008 | feller_satisfying | 90.0 | 0.5 | 12.74454 | 0.00303 | -0.00295 | -0.97 | 88.157 | 19820 | 162 | 0.0000% |
| qe | 1008 | feller_satisfying | 100.0 | 0.5 | 6.31122 | 0.00351 | -0.00130 | -0.37 | 87.478 | 19820 | 162 | 0.0000% |
| qe | 1008 | feller_satisfying | 110.0 | 0.5 | 2.38535 | 0.00279 | -0.00089 | -0.32 | 87.458 | 19820 | 162 | 0.0000% |
| qe | 1008 | feller_satisfying | 90.0 | 2.0 | 18.25761 | 0.00806 | -0.00289 | -0.36 | 86.643 | 19820 | 162 | 0.0000% |
| qe | 1008 | feller_satisfying | 100.0 | 2.0 | 12.74139 | 0.00805 | -0.00230 | -0.29 | 85.963 | 19820 | 162 | 0.0000% |
| qe | 1008 | feller_satisfying | 110.0 | 2.0 | 8.47140 | 0.00750 | -0.00136 | -0.18 | 85.952 | 19820 | 162 | 0.0000% |
| qe | 1008 | feller_violating | 90.0 | 0.5 | 12.42882 | 0.00324 | -0.00538 | -1.66 | 88.974 | 19820 | 162 | 0.0000% |
| qe | 1008 | feller_violating | 100.0 | 0.5 | 5.21021 | 0.00253 | -0.00326 | -1.29 | 88.435 | 19820 | 162 | 0.0000% |
| qe | 1008 | feller_violating | 110.0 | 0.5 | 1.08942 | 0.00164 | -0.00167 | -1.02 | 88.416 | 19820 | 162 | 0.0000% |
| qe | 1008 | feller_violating | 90.0 | 2.0 | 16.84475 | 0.00693 | -0.00654 | -0.94 | 93.547 | 19820 | 162 | 0.0000% |
| qe | 1008 | feller_violating | 100.0 | 2.0 | 10.43943 | 0.00588 | -0.00444 | -0.75 | 93.227 | 19820 | 162 | 0.0000% |
| qe | 1008 | feller_violating | 110.0 | 2.0 | 5.54932 | 0.00475 | -0.00328 | -0.69 | 93.210 | 19820 | 162 | 0.0000% |
| euler-ft | 4 | feller_satisfying | 90.0 | 0.5 | 12.75680 | 0.00348 | +0.00931 | +2.68 | 0.125 | 3200000 | 1 |  |
| euler-ft | 4 | feller_satisfying | 100.0 | 0.5 | 6.37878 | 0.00375 | +0.06626 | +17.69 | 0.125 | 3200000 | 1 |  |
| euler-ft | 4 | feller_satisfying | 110.0 | 0.5 | 2.47612 | 0.00292 | +0.08989 | +30.78 | 0.125 | 3200000 | 1 |  |
| euler-ft | 4 | feller_satisfying | 90.0 | 2.0 | 18.44522 | 0.00952 | +0.18472 | +19.40 | 0.118 | 3200000 | 1 |  |
| euler-ft | 4 | feller_satisfying | 100.0 | 2.0 | 12.96973 | 0.00903 | +0.22604 | +25.02 | 0.118 | 3200000 | 1 |  |
| euler-ft | 4 | feller_satisfying | 110.0 | 2.0 | 8.72718 | 0.00815 | +0.25442 | +31.22 | 0.118 | 3200000 | 1 |  |
| euler-ft | 4 | feller_violating | 90.0 | 0.5 | 12.58610 | 0.00374 | +0.15190 | +40.63 | 0.118 | 3200000 | 1 |  |
| euler-ft | 4 | feller_violating | 100.0 | 0.5 | 5.65515 | 0.00317 | +0.44168 | +139.29 | 0.118 | 3200000 | 1 |  |
| euler-ft | 4 | feller_violating | 110.0 | 0.5 | 1.55174 | 0.00209 | +0.46065 | +220.44 | 0.118 | 3200000 | 1 |  |
| euler-ft | 4 | feller_violating | 90.0 | 2.0 | 17.85242 | 0.00850 | +1.00113 | +117.76 | 0.117 | 3200000 | 1 |  |
| euler-ft | 4 | feller_violating | 100.0 | 2.0 | 11.71848 | 0.00766 | +1.27461 | +166.44 | 0.117 | 3200000 | 1 |  |
| euler-ft | 4 | feller_violating | 110.0 | 2.0 | 6.98184 | 0.00658 | +1.42923 | +217.34 | 0.117 | 3200000 | 1 |  |
| euler-ft | 12 | feller_satisfying | 90.0 | 0.5 | 12.74952 | 0.00264 | +0.00203 | +0.77 | 0.589 | 1538460 | 3 |  |
| euler-ft | 12 | feller_satisfying | 100.0 | 0.5 | 6.32908 | 0.00298 | +0.01656 | +5.57 | 0.589 | 1538460 | 3 |  |
| euler-ft | 12 | feller_satisfying | 110.0 | 0.5 | 2.40974 | 0.00235 | +0.02350 | +9.99 | 0.588 | 1538460 | 3 |  |
| euler-ft | 12 | feller_satisfying | 90.0 | 2.0 | 18.28438 | 0.00718 | +0.02388 | +3.33 | 0.572 | 1538460 | 3 |  |
| euler-ft | 12 | feller_satisfying | 100.0 | 2.0 | 12.77222 | 0.00695 | +0.02854 | +4.11 | 0.572 | 1538460 | 3 |  |
| euler-ft | 12 | feller_satisfying | 110.0 | 2.0 | 8.50529 | 0.00635 | +0.03253 | +5.12 | 0.572 | 1538460 | 3 |  |
| euler-ft | 12 | feller_violating | 90.0 | 0.5 | 12.48580 | 0.00289 | +0.05161 | +17.84 | 0.574 | 1538460 | 3 |  |
| euler-ft | 12 | feller_violating | 100.0 | 0.5 | 5.32765 | 0.00230 | +0.11417 | +49.61 | 0.575 | 1538460 | 3 |  |
| euler-ft | 12 | feller_violating | 110.0 | 0.5 | 1.19145 | 0.00145 | +0.10036 | +69.14 | 0.574 | 1538460 | 3 |  |
| euler-ft | 12 | feller_violating | 90.0 | 2.0 | 17.11391 | 0.00619 | +0.26262 | +42.41 | 0.576 | 1538460 | 3 |  |
| euler-ft | 12 | feller_violating | 100.0 | 2.0 | 10.78063 | 0.00534 | +0.33676 | +63.11 | 0.576 | 1538460 | 3 |  |
| euler-ft | 12 | feller_violating | 110.0 | 2.0 | 5.92958 | 0.00436 | +0.37698 | +86.53 | 0.576 | 1538460 | 3 |  |
| euler-ft | 52 | feller_satisfying | 90.0 | 0.5 | 12.74343 | 0.00297 | -0.00406 | -1.37 | 2.813 | 377358 | 9 |  |
| euler-ft | 52 | feller_satisfying | 100.0 | 0.5 | 6.31309 | 0.00342 | +0.00057 | +0.17 | 2.784 | 377358 | 9 |  |
| euler-ft | 52 | feller_satisfying | 110.0 | 0.5 | 2.38876 | 0.00272 | +0.00253 | +0.93 | 2.783 | 377358 | 9 |  |
| euler-ft | 52 | feller_satisfying | 90.0 | 2.0 | 18.25172 | 0.00795 | -0.00878 | -1.11 | 2.763 | 377358 | 9 |  |
| euler-ft | 52 | feller_satisfying | 100.0 | 2.0 | 12.73698 | 0.00787 | -0.00671 | -0.85 | 2.743 | 377358 | 9 |  |
| euler-ft | 52 | feller_satisfying | 110.0 | 2.0 | 8.46767 | 0.00730 | -0.00509 | -0.70 | 2.741 | 377358 | 9 |  |
| euler-ft | 52 | feller_violating | 90.0 | 0.5 | 12.43697 | 0.00323 | +0.00278 | +0.86 | 2.766 | 377358 | 9 |  |
| euler-ft | 52 | feller_violating | 100.0 | 0.5 | 5.22643 | 0.00253 | +0.01296 | +5.13 | 2.729 | 377358 | 9 |  |
| euler-ft | 52 | feller_violating | 110.0 | 0.5 | 1.10413 | 0.00161 | +0.01304 | +8.12 | 2.726 | 377358 | 9 |  |
| euler-ft | 52 | feller_violating | 90.0 | 2.0 | 16.87572 | 0.00691 | +0.02443 | +3.54 | 2.763 | 377358 | 9 |  |
| euler-ft | 52 | feller_violating | 100.0 | 2.0 | 10.48413 | 0.00587 | +0.04026 | +6.86 | 2.739 | 377358 | 9 |  |
| euler-ft | 52 | feller_violating | 110.0 | 2.0 | 5.60375 | 0.00472 | +0.05115 | +10.84 | 2.738 | 377358 | 9 |  |
| euler-ft | 104 | feller_satisfying | 90.0 | 0.5 | 12.74125 | 0.00302 | -0.00625 | -2.07 | 4.340 | 190476 | 17 |  |
| euler-ft | 104 | feller_satisfying | 100.0 | 0.5 | 6.30720 | 0.00349 | -0.00532 | -1.52 | 4.261 | 190476 | 17 |  |
| euler-ft | 104 | feller_satisfying | 110.0 | 0.5 | 2.38360 | 0.00278 | -0.00264 | -0.95 | 4.258 | 190476 | 17 |  |
| euler-ft | 104 | feller_satisfying | 90.0 | 2.0 | 18.24551 | 0.00807 | -0.01499 | -1.86 | 4.212 | 190476 | 17 |  |
| euler-ft | 104 | feller_satisfying | 100.0 | 2.0 | 12.73045 | 0.00803 | -0.01324 | -1.65 | 4.203 | 190476 | 17 |  |
| euler-ft | 104 | feller_satisfying | 110.0 | 2.0 | 8.46271 | 0.00746 | -0.01005 | -1.35 | 4.200 | 190476 | 17 |  |
| euler-ft | 104 | feller_violating | 90.0 | 0.5 | 12.43210 | 0.00327 | -0.00209 | -0.64 | 4.205 | 190476 | 17 |  |
| euler-ft | 104 | feller_violating | 100.0 | 0.5 | 5.21439 | 0.00256 | +0.00092 | +0.36 | 4.204 | 190476 | 17 |  |
| euler-ft | 104 | feller_violating | 110.0 | 0.5 | 1.09389 | 0.00163 | +0.00280 | +1.72 | 4.201 | 190476 | 17 |  |
| euler-ft | 104 | feller_violating | 90.0 | 2.0 | 16.84816 | 0.00701 | -0.00313 | -0.45 | 4.168 | 190476 | 17 |  |
| euler-ft | 104 | feller_violating | 100.0 | 2.0 | 10.44848 | 0.00595 | +0.00461 | +0.77 | 4.167 | 190476 | 17 |  |
| euler-ft | 104 | feller_violating | 110.0 | 2.0 | 5.56514 | 0.00478 | +0.01254 | +2.62 | 4.163 | 190476 | 17 |  |
| euler-ft | 252 | feller_satisfying | 90.0 | 0.5 | 12.74262 | 0.00302 | -0.00488 | -1.62 | 9.255 | 79050 | 41 |  |
| euler-ft | 252 | feller_satisfying | 100.0 | 0.5 | 6.30672 | 0.00350 | -0.00580 | -1.66 | 9.228 | 79050 | 41 |  |
| euler-ft | 252 | feller_satisfying | 110.0 | 0.5 | 2.38409 | 0.00278 | -0.00215 | -0.77 | 9.225 | 79050 | 41 |  |
| euler-ft | 252 | feller_satisfying | 90.0 | 2.0 | 18.24823 | 0.00805 | -0.01227 | -1.52 | 9.417 | 79050 | 41 |  |
| euler-ft | 252 | feller_satisfying | 100.0 | 2.0 | 12.73246 | 0.00802 | -0.01123 | -1.40 | 9.260 | 79050 | 41 |  |
| euler-ft | 252 | feller_satisfying | 110.0 | 2.0 | 8.46494 | 0.00746 | -0.00783 | -1.05 | 9.257 | 79050 | 41 |  |
| euler-ft | 252 | feller_violating | 90.0 | 0.5 | 12.42866 | 0.00325 | -0.00554 | -1.70 | 9.308 | 79050 | 41 |  |
| euler-ft | 252 | feller_violating | 100.0 | 0.5 | 5.21115 | 0.00254 | -0.00232 | -0.91 | 9.218 | 79050 | 41 |  |
| euler-ft | 252 | feller_violating | 110.0 | 0.5 | 1.09179 | 0.00163 | +0.00070 | +0.43 | 9.216 | 79050 | 41 |  |
| euler-ft | 252 | feller_violating | 90.0 | 2.0 | 16.84126 | 0.00697 | -0.01003 | -1.44 | 9.305 | 79050 | 41 |  |
| euler-ft | 252 | feller_violating | 100.0 | 2.0 | 10.43828 | 0.00592 | -0.00559 | -0.94 | 9.238 | 79050 | 41 |  |
| euler-ft | 252 | feller_violating | 110.0 | 2.0 | 5.55253 | 0.00476 | -0.00008 | -0.02 | 9.235 | 79050 | 41 |  |
| euler-ft | 504 | feller_satisfying | 90.0 | 0.5 | 12.74493 | 0.00303 | -0.00256 | -0.85 | 15.786 | 39602 | 81 |  |
| euler-ft | 504 | feller_satisfying | 100.0 | 0.5 | 6.31025 | 0.00351 | -0.00227 | -0.65 | 15.513 | 39602 | 81 |  |
| euler-ft | 504 | feller_satisfying | 110.0 | 0.5 | 2.38495 | 0.00279 | -0.00128 | -0.46 | 15.504 | 39602 | 81 |  |
| euler-ft | 504 | feller_satisfying | 90.0 | 2.0 | 18.25387 | 0.00808 | -0.00662 | -0.82 | 15.582 | 39602 | 81 |  |
| euler-ft | 504 | feller_satisfying | 100.0 | 2.0 | 12.73713 | 0.00806 | -0.00655 | -0.81 | 15.348 | 39602 | 81 |  |
| euler-ft | 504 | feller_satisfying | 110.0 | 2.0 | 8.46704 | 0.00750 | -0.00572 | -0.76 | 15.337 | 39602 | 81 |  |
| euler-ft | 504 | feller_violating | 90.0 | 0.5 | 12.43264 | 0.00326 | -0.00155 | -0.48 | 15.921 | 39602 | 81 |  |
| euler-ft | 504 | feller_violating | 100.0 | 0.5 | 5.21325 | 0.00255 | -0.00023 | -0.09 | 15.626 | 39602 | 81 |  |
| euler-ft | 504 | feller_violating | 110.0 | 0.5 | 1.09147 | 0.00164 | +0.00038 | +0.23 | 15.616 | 39602 | 81 |  |
| euler-ft | 504 | feller_violating | 90.0 | 2.0 | 16.84467 | 0.00699 | -0.00662 | -0.95 | 15.731 | 39602 | 81 |  |
| euler-ft | 504 | feller_violating | 100.0 | 2.0 | 10.43932 | 0.00593 | -0.00455 | -0.77 | 15.439 | 39602 | 81 |  |
| euler-ft | 504 | feller_violating | 110.0 | 2.0 | 5.55110 | 0.00478 | -0.00151 | -0.32 | 15.429 | 39602 | 81 |  |
| euler-ft | 1008 | feller_satisfying | 90.0 | 0.5 | 12.74570 | 0.00303 | -0.00179 | -0.59 | 32.266 | 19820 | 162 |  |
| euler-ft | 1008 | feller_satisfying | 100.0 | 0.5 | 6.31295 | 0.00351 | +0.00043 | +0.12 | 31.563 | 19820 | 162 |  |
| euler-ft | 1008 | feller_satisfying | 110.0 | 0.5 | 2.38677 | 0.00279 | +0.00054 | +0.19 | 31.561 | 19820 | 162 |  |
| euler-ft | 1008 | feller_satisfying | 90.0 | 2.0 | 18.25771 | 0.00807 | -0.00279 | -0.35 | 32.253 | 19820 | 162 |  |
| euler-ft | 1008 | feller_satisfying | 100.0 | 2.0 | 12.74320 | 0.00806 | -0.00049 | -0.06 | 31.492 | 19820 | 162 |  |
| euler-ft | 1008 | feller_satisfying | 110.0 | 2.0 | 8.47171 | 0.00750 | -0.00105 | -0.14 | 31.490 | 19820 | 162 |  |
| euler-ft | 1008 | feller_violating | 90.0 | 0.5 | 12.42912 | 0.00325 | -0.00507 | -1.56 | 32.380 | 19820 | 162 |  |
| euler-ft | 1008 | feller_violating | 100.0 | 0.5 | 5.21281 | 0.00254 | -0.00067 | -0.26 | 31.653 | 19820 | 162 |  |
| euler-ft | 1008 | feller_violating | 110.0 | 0.5 | 1.09026 | 0.00164 | -0.00083 | -0.51 | 31.647 | 19820 | 162 |  |
| euler-ft | 1008 | feller_violating | 90.0 | 2.0 | 16.84467 | 0.00697 | -0.00662 | -0.95 | 34.516 | 19820 | 162 |  |
| euler-ft | 1008 | feller_violating | 100.0 | 2.0 | 10.44257 | 0.00591 | -0.00129 | -0.22 | 33.857 | 19820 | 162 |  |
| euler-ft | 1008 | feller_violating | 110.0 | 2.0 | 5.55226 | 0.00476 | -0.00034 | -0.07 | 33.854 | 19820 | 162 |  |
