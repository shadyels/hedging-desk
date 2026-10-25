"""`portfolio/aggregate.py`: unit scaling, q labelling (G-LBL), hedge targets, bands, republish."""

from __future__ import annotations

import pytest
from _book_fixtures import LOADABLE, real_config, real_universe
from hypothesis import given
from hypothesis import strategies as st

from exo.greeks.bump import Estimate, Greeks
from exo.portfolio.aggregate import (
    band_for,
    flagged,
    hedge_targets,
    line_values,
    should_publish,
)
from exo.portfolio.book import BookProduct
from exo.refdata import Instrument

CFG = real_config()
UNI = real_universe(CFG)


def _inst(symbol: str) -> Instrument:
    inst = UNI.instrument_by_symbol(symbol)
    assert inst is not None
    return inst


def _product(symbol: str = "AAPL", qty: float = -1.0, pid: str = "P1") -> BookProduct:
    payoff, _ = LOADABLE["autocallable" if symbol != "EURUSD" else "tarf"]
    return BookProduct(pid, "autocallable", qty, payoff, _inst(symbol))


def _greeks(**over: tuple[float, float]) -> Greeks:
    base = {
        "pv": (100.0, 1.0), "delta": (8000.0, 100.0), "gamma": (4.0, 1.0), "vega": (2000.0, 10.0),
        "rho": (3000.0, 30.0), "dq": (-50_000.0, 100.0), "theta": (-10.0, 0.1),
    }  # fmt: skip
    base.update(over)
    return Greeks(**{k: Estimate(*v) for k, v in base.items()})


def test_flagged_boundary_zero_and_non_finite() -> None:
    assert not flagged("delta", Estimate(100.0, 2.0), 0.02)  # se == max_rel * |v|: not flagged
    assert flagged("delta", Estimate(100.0, 2.0000001), 0.02)
    assert not flagged("delta", Estimate(-100.0, 1.9), 0.02)
    assert flagged("delta", Estimate(0.0, 0.0), 0.02)  # a zero value is always flagged
    assert flagged("delta", Estimate(-0.0, 1e-30), 0.02)
    with pytest.raises(ValueError, match="delta"):
        flagged("delta", Estimate(float("nan"), 1.0), 0.02)


def test_line_values_scales_to_adr014_units_with_desk_sign() -> None:
    prod = _product("AAPL", qty=-2.0)
    ln = line_values(prod, _greeks(), spot=187.51, max_rel_std_error=0.02)
    assert ln.pv == Estimate(-200.0, 2.0)
    assert ln.delta == Estimate(-16_000.0, 200.0)
    assert ln.gamma.value == pytest.approx(-2.0 * 4.0 * 0.01 * 187.51)  # q * Gamma * 0.01 * S
    assert ln.gamma.std_err == pytest.approx(2.0 * 1.0 * 0.01 * 187.51)
    assert ln.vega.value == pytest.approx(-40.0)
    assert ln.vega.std_err == pytest.approx(0.2)
    assert ln.rho.value == pytest.approx(-2.0 * 3000.0 * 1e-4)
    assert ln.theta == Estimate(20.0, 0.2)
    assert ln.div_sens.value == pytest.approx(-2.0 * -50_000.0 * 0.01)
    assert ln.div_sens.std_err >= 0 and ln.rho.std_err >= 0
    assert (ln.product_id, ln.product_type, ln.instrument.symbol) == ("P1", "autocallable", "AAPL")


@pytest.mark.parametrize("symbol", ["AAPL", "SPX", "SPY"])
def test_g_lbl_equity_index_etf_label_q_as_dividend_sensitivity(symbol: str) -> None:
    ln = line_values(_product(symbol, qty=1.0), _greeks(), spot=100.0, max_rel_std_error=0.02)
    assert ln.div_sens.value == pytest.approx(0.01 * -50_000.0)
    assert ln.rho_foreign == Estimate(0.0, 0.0)
    assert "rho_foreign" not in ln.flagged


def test_g_lbl_fx_spot_labels_q_as_foreign_rho_per_bp() -> None:
    ln = line_values(_product("EURUSD", qty=1.0), _greeks(), spot=1.08, max_rel_std_error=0.02)
    assert ln.rho_foreign.value == pytest.approx(1e-4 * -50_000.0)
    assert ln.rho_foreign.std_err == pytest.approx(1e-4 * 100.0)
    assert ln.div_sens == Estimate(0.0, 0.0)
    assert "div_sens" not in ln.flagged


def test_g_lbl_other_classes_are_an_error() -> None:
    with pytest.raises(ValueError, match="no q label"):
        line_values(_product("ESZ6"), _greeks(), spot=5500.0, max_rel_std_error=0.02)


def test_flagged_names_hold_only_applicable_greeks_and_at_most_six() -> None:
    wide = _greeks(
        delta=(1.0, 1.0), gamma=(1.0, 1.0), vega=(1.0, 1.0), rho=(1.0, 1.0), theta=(1.0, 1.0),
        dq=(1.0, 1.0),
    )  # fmt: skip
    ln = line_values(_product("AAPL"), wide, spot=100.0, max_rel_std_error=0.02)
    assert ln.flagged == ("delta", "gamma", "vega", "rho", "theta", "div_sens")
    fx = line_values(_product("EURUSD"), wide, spot=1.0, max_rel_std_error=0.02)
    assert fx.flagged == ("delta", "gamma", "vega", "rho", "theta", "rho_foreign")
    tight = line_values(
        _product("AAPL"), _greeks(delta=(1000.0, 1.0)), spot=100.0, max_rel_std_error=0.02
    )
    assert "delta" not in tight.flagged


def test_band_mapping_by_class() -> None:
    assert band_for(_inst("AAPL"), CFG.rehedge) == CFG.rehedge.band_equity_qty_e2
    assert band_for(_inst("SPX"), CFG.rehedge) == CFG.rehedge.band_equity_qty_e2  # INDEX
    assert band_for(_inst("SPY"), CFG.rehedge) == CFG.rehedge.band_equity_qty_e2  # ETF
    assert band_for(_inst("EURUSD"), CFG.rehedge) == CFG.rehedge.band_fx_qty_e2
    with pytest.raises(ValueError, match="no band"):
        band_for(_inst("ESZ6"), CFG.rehedge)


def test_hedge_target_is_minus_desk_delta_rounded_once_to_the_lot() -> None:
    # desk issued one note: client delta +8044.65 -> desk delta -8044.65 -> target +8044.65 -> 8045
    ln = line_values(
        _product("AAPL", qty=-1.0),
        _greeks(delta=(8044.65, 580.0)),
        spot=187.5,
        max_rel_std_error=0.02,
    )
    assert hedge_targets([ln]) == {1001: 804_500}
    # exact tie 8044.5 -> half-even to 8044 shares
    tie = line_values(
        _product("AAPL", qty=-1.0),
        _greeks(delta=(8044.5, 580.0)),
        spot=187.5,
        max_rel_std_error=0.02,
    )
    assert hedge_targets([tie]) == {1001: 804_400}


def test_hedge_targets_sum_lines_per_underlying_before_rounding() -> None:
    # two lines each +0.5 share: rounding per line would give 0 + 0 (half-even), summing gives 1
    a = line_values(
        _product("AAPL", qty=-1.0, pid="A"),
        _greeks(delta=(0.5, 1.0)),
        spot=1.0,
        max_rel_std_error=1,
    )
    b = line_values(
        _product("AAPL", qty=-1.0, pid="B"),
        _greeks(delta=(0.5, 1.0)),
        spot=1.0,
        max_rel_std_error=1,
    )
    c = line_values(
        _product("MSFT", qty=1.0, pid="C"),
        _greeks(delta=(300.0, 1.0)),
        spot=1.0,
        max_rel_std_error=1,
    )
    assert hedge_targets([a, b, c]) == {1001: 100, 1002: -30_000}
    assert hedge_targets([]) == {}


def test_should_publish_rule() -> None:
    assert should_publish(100, None, 50, unhalted=False)  # nothing published yet
    assert not should_publish(150, 100, 50, unhalted=False)  # |delta| == band: not published
    assert should_publish(151, 100, 50, unhalted=False)
    assert should_publish(49, 100, 50, unhalted=False)
    assert not should_publish(100, 100, 50, unhalted=False)
    assert should_publish(100, 100, 50, unhalted=True)  # un-halt bypasses the band


@given(st.integers(-(10**9), 10**9), st.integers(-(10**9), 10**9), st.integers(1, 10**6))
def test_should_publish_is_symmetric_in_new_and_last(new: int, last: int, band: int) -> None:
    assert should_publish(new, last, band, unhalted=False) == should_publish(
        last, new, band, unhalted=False
    )
