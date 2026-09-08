"""Tests for exo.models.analytic: Black-Scholes closed forms.

Reference values below were computed independently with `math.erf`, not with
`scipy.special.ndtr`, so this test does not validate the module against itself.
"""

import math

from hypothesis import given
from hypothesis import strategies as st

from exo.models.analytic import bs_call_price, bs_put_price

# Hull's textbook example (no dividend): S=42, K=40, r=10%, sigma=20%, T=0.5y.
_HULL_CALL = 4.759422392871535
_HULL_PUT = 0.8085993729000975

# With a continuous dividend yield: S=K=100, r=2%, q=1%, sigma=20%, T=1y.
# This is also root CLAUDE.md's cross-check value for the Heston CF's xi->0 limit.
_DIV_CALL = 8.34940576709677
_DIV_PUT = 7.364289722855496


def test_bs_call_price_matches_independent_reference_no_dividend() -> None:
    price = bs_call_price(s0=42.0, strike=40.0, r=0.10, q=0.0, sigma=0.20, expiry=0.5)
    assert math.isclose(price, _HULL_CALL, rel_tol=1e-12)


def test_bs_put_price_matches_independent_reference_no_dividend() -> None:
    price = bs_put_price(s0=42.0, strike=40.0, r=0.10, q=0.0, sigma=0.20, expiry=0.5)
    assert math.isclose(price, _HULL_PUT, rel_tol=1e-12)


def test_bs_call_price_matches_independent_reference_with_dividend() -> None:
    price = bs_call_price(s0=100.0, strike=100.0, r=0.02, q=0.01, sigma=0.20, expiry=1.0)
    assert math.isclose(price, _DIV_CALL, rel_tol=1e-12)


def test_bs_put_price_matches_independent_reference_with_dividend() -> None:
    price = bs_put_price(s0=100.0, strike=100.0, r=0.02, q=0.01, sigma=0.20, expiry=1.0)
    assert math.isclose(price, _DIV_PUT, rel_tol=1e-12)


@given(
    s0=st.floats(min_value=1.0, max_value=1000.0),
    strike=st.floats(min_value=1.0, max_value=1000.0),
    r=st.floats(min_value=-0.05, max_value=0.20),
    q=st.floats(min_value=-0.05, max_value=0.20),
    sigma=st.floats(min_value=0.01, max_value=2.0),
    expiry=st.floats(min_value=0.01, max_value=10.0),
)
def test_put_call_parity(
    s0: float, strike: float, r: float, q: float, sigma: float, expiry: float
) -> None:
    call = bs_call_price(s0=s0, strike=strike, r=r, q=q, sigma=sigma, expiry=expiry)
    put = bs_put_price(s0=s0, strike=strike, r=r, q=q, sigma=sigma, expiry=expiry)
    forward_parity = s0 * math.exp(-q * expiry) - strike * math.exp(-r * expiry)
    assert math.isclose(call - put, forward_parity, rel_tol=1e-9, abs_tol=1e-9)


def test_expiry_zero_is_intrinsic_itm_call() -> None:
    price = bs_call_price(s0=110.0, strike=100.0, r=0.05, q=0.0, sigma=0.20, expiry=0.0)
    assert price == 10.0


def test_expiry_zero_is_intrinsic_otm_call() -> None:
    price = bs_call_price(s0=90.0, strike=100.0, r=0.05, q=0.0, sigma=0.20, expiry=0.0)
    assert price == 0.0


def test_expiry_zero_is_intrinsic_put() -> None:
    price = bs_put_price(s0=90.0, strike=100.0, r=0.05, q=0.0, sigma=0.20, expiry=0.0)
    assert price == 10.0


def test_sigma_zero_is_discounted_forward_intrinsic_call() -> None:
    s0, strike, r, q, expiry = 100.0, 95.0, 0.03, 0.01, 2.0
    forward = s0 * math.exp((r - q) * expiry)
    expected = math.exp(-r * expiry) * max(forward - strike, 0.0)
    price = bs_call_price(s0=s0, strike=strike, r=r, q=q, sigma=0.0, expiry=expiry)
    assert math.isclose(price, expected, rel_tol=1e-12)


def test_sigma_zero_is_discounted_forward_intrinsic_put() -> None:
    s0, strike, r, q, expiry = 100.0, 105.0, 0.03, 0.01, 2.0
    forward = s0 * math.exp((r - q) * expiry)
    expected = math.exp(-r * expiry) * max(strike - forward, 0.0)
    price = bs_put_price(s0=s0, strike=strike, r=r, q=q, sigma=0.0, expiry=expiry)
    assert math.isclose(price, expected, rel_tol=1e-12)
