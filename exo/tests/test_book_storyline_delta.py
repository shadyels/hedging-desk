"""Storyline term-sheet materiality (ADR-006 Amendment 9 s7; docs/ARCHITECTURE.md storyline step 1).

The shipped `book.toml` autocallable (desk-issued, -1x of USD 10m) is repriced with the REAL
`compute_greeks` at the pre-gap mid (187.51) and the post-gap mid (176.00), with the book's own
seed (20261024) and 20,000 paths, 252 steps, QE, production bumps. The gap must

1. move the client-side delta by more than 4 combined standard errors (so the hedge change is the
   model talking, not MC noise);
2. move the lot-rounded hedge target by more than the 500-share EXO band (so EXO republishes);
3. leave desk vega, |gamma| and rho under the `d1.toml` WARN limits at both spots (so the
   storyline triggers no limit alert).

MEASURED by this implementation (seed 20261024, 20,000 paths, 252 steps, QE, production bumps):
- client-side delta 7,893 +/- 590 shares at 187.51 and 12,472 +/- 682 at 176.00: a +4,579 share jump
  against a hypot SE of 902, z = 5.08 (threshold 4). The architect's prototype (seed 4242) read
  5,710 against 928, z = 6.2: the two seeds differ by about 1.2 combined SE, as MC noise predicts.
  `hypot` ignores the positive CRN correlation between the two spots, so it is conservative.
- lot-rounded hedge target 789,300 -> 1,247,200 (qty_e2), i.e. +4,579 shares against the 500-share
  band.
- desk vega 22,482 -> 33,335 USD per vol point (warn 50,000); |gamma| 1,132 -> 1,953 shares per
  +1% (warn 20,000); rho 352 -> 423 USD per bp (warn 10,000).
- relative SEs, 187.51 / 176.00: delta 7.5% / 5.5%; vega 7.0% / 5.2%; rho 5.4% / 7.3%;
  div_sens 21% / 15%; gamma 103% / 70%; theta 99% / 3,208% (theta at 176.00 is near zero).
  Every Greek is therefore published flagged at `max_rel_std_error = 0.02` (Amendment 9 s7),
  recorded honestly rather than tuned away.
"""

from __future__ import annotations

import functools
import math
import tomllib
from pathlib import Path

from _book_fixtures import BOOK_TOML, real_config, real_universe

from exo.greeks.bump import compute_greeks
from exo.portfolio.aggregate import LineValues, hedge_targets, line_values
from exo.portfolio.book import load_book

CFG = real_config()
D1_TOML = Path(__file__).resolve().parents[2] / "delta-one" / "d1.toml"
PRE_GAP, POST_GAP = 187.51, 176.00


@functools.cache
def _line(mid: float) -> tuple[LineValues, float]:
    """(desk-signed line, client-side delta SE) at `mid`."""
    (book,) = load_book(BOOK_TOML, CFG, real_universe(CFG))
    product = book.products[0]
    params = CFG.models.params["AAPL"].model_copy(update={"s0": mid})
    greeks = compute_greeks(
        product.payoff,
        params,
        scheme=CFG.models.defaults.scheme,
        n_steps_per_year=CFG.models.defaults.n_steps_per_year,
        n_paths=book.n_paths,
        seed=book.seed,
        bumps=CFG.greeks,
    )
    line = line_values(product, greeks, spot=mid, max_rel_std_error=CFG.greeks.max_rel_std_error)
    return line, greeks.delta.std_err


def test_gap_moves_client_delta_by_more_than_four_combined_standard_errors() -> None:
    (pre, se_pre), (post, se_post) = _line(PRE_GAP), _line(POST_GAP)
    client_pre, client_post = -pre.delta.value, -post.delta.value  # desk_quantity = -1
    jump = client_post - client_pre
    z = jump / math.hypot(se_pre, se_post)
    assert z > 4.0, f"client delta {client_pre:,.0f} -> {client_post:,.0f}, jump z={z:.2f}"


def test_gap_moves_the_lot_rounded_hedge_target_by_more_than_the_band() -> None:
    (pre, _), (post, _) = _line(PRE_GAP), _line(POST_GAP)
    t_pre = hedge_targets([pre])[1001]
    t_post = hedge_targets([post])[1001]
    assert t_post > t_pre > 0  # the desk is short the note, so it holds shares
    assert t_pre % 100 == 0 and t_post % 100 == 0  # lot-rounded
    assert t_post - t_pre > CFG.rehedge.band_equity_qty_e2  # 50_000 e2 = 500 shares


def test_storyline_vega_gamma_rho_stay_under_the_d1_warn_limits() -> None:
    limits = tomllib.loads(D1_TOML.read_text())["limits"]["defaults"]
    for mid in (PRE_GAP, POST_GAP):
        line, _ = _line(mid)
        assert abs(line.vega.value) * 1e9 < limits["vega_warn_e9"], (mid, line.vega.value)
        assert abs(line.gamma.value) * 100 < limits["gamma_warn_qty_e2"], (mid, line.gamma.value)
        assert abs(line.rho.value) * 1e9 < limits["rho_warn_e9"], (mid, line.rho.value)


def test_storyline_greeks_are_published_flagged_at_20k_paths() -> None:
    """Amendment 9 s7, recorded not tuned away: delta SE is several % of delta."""
    for mid in (PRE_GAP, POST_GAP):
        line, _ = _line(mid)
        assert set(line.flagged) == {"delta", "gamma", "vega", "rho", "theta", "div_sens"}
