# Gate margin audit — are any other validation gates passing on insufficient power?

**STATUS: AUDITED 2026-09-29 @ afa3298. No gate is biased; no configuration or tolerance
changed.** Results are in the section at the end; the brief below is kept as the method. Written
2026-09-25 during P2.M1 slice 3 (`feat/exo-qmc-cv`), which found one instance and fixed it.
Nothing here is urgent; it is a correctness audit of the acceptance surface, not a blocker for any
milestone.

## Why this exists

Slice 3 added quasi-Monte Carlo, whose tighter error bar made a **pre-existing** defect resolvable:
**G1's `euler-ft` arm was returning PASS for a configuration that violates the acceptance test it
implements.**

`exo/CLAUDE.md` makes "MC vs closed form within 3 standard errors" a *blocking* gate. G1 ran
full-truncation Euler at `n_steps=50`. ADR-006 Amendment 4 §4's own convergence study records Euler's
**first-passing step count as 104** at this Feller ratio (0.333) — so 50 is below convergence.
Measured:

| config | mean z over 20 seeds | max \|z\| | pass count |
|---|---|---|---|
| `euler-ft`, n_steps=50 (as shipped) | **+1.02** | **2.73** | 20/20 |
| `euler-ft`, n_steps=104 (the fix) | +0.193 | 1.970 | 20/20 |
| `qe`, n_steps=50 (untouched control) | −0.076 | 1.921 | 20/20 |

It passed every seed while sitting ~0.27σ from red, against a real +0.058 bias against the
characteristic function (confirmed independently at 200k and 400k paths). **A gate that passes at a
systematic +1σ offset is not a gate; it is a coin-flip that currently lands heads.**

**The open question this brief answers:** G1 was one of several gates whose `tol_abs` was measured
the same way, at the same path count, at the same time. **Nobody has checked the margin on the
others.** One of four turned out bad.

## Targets

Only gates with a **3-SE bias conjunct** are auditable. `G3a` and `G5` are exact per-path identities
(`se == 0` / algebraic) — there is nothing to measure; skip them.

| Gate | File | Reference | `tol_abs` | Config |
|---|---|---|---|---|
| G1 | `exo/tests/test_validation_gates.py` | `heston_vanilla_price` | 0.06 | qe@50, euler-ft@104 — **already audited, fixed** |
| **G2** | `exo/tests/test_validation_gates.py` | BS closed form, Heston degenerated | 0.09 | n_steps=50, 20k paths |
| **X** | `exo/tests/test_validation_gates.py` | QE vs Euler, paired | 0.08 | n_steps=500 |
| **G3b** | `exo/tests/test_validation_gates_products.py` | short-put decomposition | 0.8 | n_steps=50, 20k paths |
| **G4** | `exo/tests/test_validation_gates_products.py` | `bs_barrier_price` | 0.09 | already parametrized over 4 seeds |
| **G4 companion** | `exo/tests/test_validation_gates_products.py` | bridge vs discrete, paired | 0.025 | — |
| G1-Q / G2-Q / G4-Q / G4-Q-CV / G3b-Q | `exo/tests/test_validation_gates_qmc.py` | as above | copied verbatim | R=64 × 1024 |

The RQMC gates are lower priority: they were written in slice 3 with a 20-seed anti-flake sweep
already run (all 20/20), so they have *some* margin evidence. The pseudo-random gates have none.

## Method

For each target, at its **committed configuration**, sweep ≥20 seeds and record the distribution of
`z = (pv − reference) / std_err`:

```python
# shape only -- read each gate for its real params, payoff and reference
for seed in range(1, 21):
    bundle = simulate(PARAMS, ENGINE, PseudoRandomSource(seed=seed))
    result = mc_estimate(bundle, discounted_payoff(bundle))
    z = (result.pv - REFERENCE) / result.std_err
```

Report per gate: **mean z, max |z|, pass count at 3 SE, and the SE range.**

**Reading the result.** A correctly-configured gate centres near zero — the `qe@50` control above
(mean −0.08) is what healthy looks like. Treat as suspect: `|mean z| ≳ 0.5`, or `max |z| > 2.5`, or a
pass count below 20/20.

**Then separate bias from noise, because the remedies are opposite.** Re-run the suspect gate at
**4–10× the path count**:
- **|z| grows** → real bias. The SE shrank and the offset did not. This is G1's signature.
- **|z| stays flat or shrinks** → sampling noise; the gate is merely tight, not wrong.

## If you find a biased gate

**Diagnose before fixing — there are three distinct causes and they need different fixes:**

1. **The discretization is unconverged at that `n_steps`** (G1's cause). Check ADR-006 Amendment 4
   §4's convergence table for that scheme's first-passing step count at the relevant Feller ratio.
   Fix = raise `n_steps` to the ADR's own number, parametrized per scheme as G1 now does.
2. **The reference pricer is the biased one.** `exo/src/exo/models/heston_cf.py` carries a ponytail
   marker: fixed integration limits `_U_MAX=200.0`, `_EPSABS=_EPSREL=1e-10`, **no adaptive
   fallback**, with its stated ceiling being "the equity parameter ranges this slice and P2.M2's
   warrant gate exercise". A persistent small offset may be the characteristic-function quadrature,
   not the Monte Carlo. Cross-check against a second reference (`bs_call_price` under a
   BS-degenerate parameter set) before blaming the MC.
3. **The payoff or fixture is subtly wrong.** Least likely — slice 2's gates were reviewed hard —
   but check the term sheet against the reference's assumptions (continuous vs discrete monitoring
   is the classic one; ADR-006 Amendment 5 §2 records that trap costing 4.01 SE).

**Never fix it by changing `tol_abs`.** This is the load-bearing rule of the whole gate culture, and
it is stated in `exo/tests/test_validation_gates_qmc.py`'s module docstring: *the path budget is the
free variable; the tolerance literal is not.* A tolerance re-measured to fit a failing configuration
is indistinguishable in a diff from a legitimate retune, and it destroys the gate silently.

## Note on why this is subtle

A "within 3 SE" gate is a claim about bias **relative to your own error bar**, so *anything that
shrinks the error bar strengthens the claim the gate makes*. That is why slice 3's variance reduction
turned a green gate red without anything having broken — and why the same audit may surface more as
later milestones tighten estimators further. Expect this to recur; it is the gate culture working,
not a regression.

## Gate

```
cd exo && uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest
```
291 tests pass as of `feat/exo-qmc-cv`.

## Where to record what you find

- **Nothing biased:** append a short results section here, set STATUS to audited with the date and
  the commit audited, and remove the pointer row from `docs/PONYTAIL-DEBT.md`.
- **Something biased:** fix the configuration (never the tolerance), record the before/after z
  statistics in the test's own docstring the way G1 now does, and add an amendment note to
  `docs/adr/ADR-006-exotics-model-stack.md` — Amendment 6 §7 is the precedent for how the G1 case
  was written up.

## Results (2026-09-29, audited at afa3298)

Setup: scope was the pseudo-random gates G2 (both scheme arms), X, G3b, G4 and the G4 companion.
G1 was already fixed, G3a/G5 are exact identities, and RQMC gates were out of scope by decision.
Each gate body was copied verbatim, with fixtures imported from the test modules and only the seed
parametrized, at its committed configuration (20,000 antithetic paths; n_steps 50, X 500). Both
assert conjuncts were evaluated (`0 < se < tol_abs` and the 3-SE / must-differ conjunct).
Environment: numpy 2.2.6, Python 3.13.5. The sweep script was a scratch artifact and is not
committed. To reproduce, copy the gate body and parametrize its seed.

Validity check: every committed seed reproduced its docstring figure before the sweep was trusted:
G2 seed 123 z = −0.471 (docstring gives |z| ≈ 0.47, unsigned); X seed 77 z = −1.310; G3b seed 202
z = −0.069; G4 seeds 42/7/123/2024 z = −1.539/+0.439/−0.443/+0.153; G4 companion seed 42
z = +23.825. An independent re-derivation of G2 and G4 from the test source (not from the sweep
script) reproduced the same per-seed and aggregate figures. The X figures at 100k and the
per-scheme split are single-source (the sweep script, validated by the X seed-77 reproduction).

**Table 1: Seeds 1–20, committed configuration**

| Gate | mean z | max \|z\| | pass | SE range | `tol_abs` |
|---|---|---|---|---|---|
| G2 `qe` | +0.393 | 1.589 | 20/20 | 0.0724–0.0756 | 0.09 |
| G2 `euler-ft` | +0.393 | 1.589 | 20/20 | 0.0724–0.0756 | 0.09 |
| X | +0.314 | 1.955 | 20/20 | 0.0588–0.0608 | 0.08 |
| G3b | −0.234 | 2.013 | 20/20 | 0.7062–0.7296 | 0.8 |
| G4 | +0.345 | 1.711 | 20/20 | 0.0767–0.0799 | 0.09 |
| G4 companion (must-differ) | min z +22.88 | — | 20/20 | 0.0168–0.0197 | 0.025 |

No gate met a suspect criterion (|mean z| ≳ 0.5, max |z| > 2.5, pass < 20/20). Because the
companion is a must-differ gate, the mean-z rule doesn't apply to it; it is judged on min z
(22.88, far above 3).

**Table 2: Bias-vs-noise discriminator at 5× paths (100,000)**

Run on all four bias-conjunct gates (G2 `qe`, G4, G3b, X) although none met a suspect criterion,
because G2, G4 and X all leaned positive. G2 `euler-ft` is omitted per finding 3; the companion is
omitted because it is a must-differ gate with min z 22.88. Pass counts evaluate both conjuncts.

| Gate | seeds | mean z | max \|z\| | pass |
|---|---|---|---|---|
| G2 `qe` | 1–10 | −0.029 | 2.016 | 10/10 |
| G4 | 1–10 | −0.093 | 2.225 | 10/10 |
| G3b | 1–10 | +0.162 | 1.719 | 10/10 |
| X | 1–10 | +0.591 | 2.667 | 10/10 |
| X | 1–40 | −0.070 | 2.715 | 40/40 |

**Findings:**

1. **G2 and G4 share one lean, not two.** Within the sweep they run the same degenerate parameters
   (G2's inline `HestonParams` equals `_DEGENERATE`), scheme, step count and seed, differing only
   in payoff, so their z are correlated (+0.86 at 20k over seeds 1–20, +0.95 at 100k). At 100k
   both means collapse to ≈0, so the lean was noise.

2. **X's 100k excursion was a run of high seeds, not bias.** Seeds 1–10 gave +0.448 at 20k and
   +0.591 at 100k, crossing the suspect line, which triggered the discriminator rather than
   constituting a bias finding. The test it performs: if X's 20k lean (+0.314 over seeds 1–20)
   were bias, the mean z at 100k would scale by √5 to ≈ +0.70. Over seeds 1–40 at 100k (seeds
   1–10 included) the observed mean is −0.070 with standard error 1/√40 ≈ 0.158, about 4.9σ
   below that prediction. This is the "|z| grows" test applied to the seed population rather than
   to fixed seeds, whose draws are not paired across path counts anyway. The 40-seed max |z| of
   2.715 exceeds the 2.5 threshold, but that threshold was set for 20 seeds: the expected max |z|
   over 40 N(0,1) draws is ≈ 2.43, and P(max |z| > 2.715) ≈ 23%. The per-scheme split (each scheme
   alone vs `heston_vanilla_price` = 7.272096, n_steps 500, 100,000 paths, seeds 1–20) found no
   significant bias in either arm: `qe` +0.00310 ± 0.00491, `euler-ft` −0.00706 ± 0.00548 (pooled
   pv − ref ± std/√20). A 400,000-path run was abandoned because it was infeasible on memory:
   resident memory reached ~5.7 GB on one seed. The engine retains the full path matrix, so
   pooling more 100k seeds was used instead.

3. **G2's two arms are numerically indistinguishable** (per-seed z differ by at most 0.0006 over
   seeds 1–20): at ξ = 1e-4 the schemes do not diverge, so the `euler-ft` arm adds no independent
   evidence. Recorded as an observation only; whether to change it is undecided.

4. **Headroom of SE under `tol_abs` is thin but not a defect.** The largest SE is 84% of `tol_abs`
   for G2, 76% for X, 91% for G3b, 89% for G4 and 79% for the G4 companion. The `tol_abs` bound is
   doing its job of certifying precision; this is not bias, and per this brief the tolerance is
   never retuned.

The `docs/PONYTAIL-DEBT.md` "Open audit — gate margins" pointer is removed by this change.
