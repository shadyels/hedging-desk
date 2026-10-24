"""G-MEM: the measured peak-memory constant of one Greek job, and the load-time guard built on it.

`GREEKS_PEAK_ARRAY_RATIO` (models/memory.py) is MEASURED here, not re-derived: `tracemalloc` around
the real `compute_greeks` (warm-up call outside the trace, so scipy's lazy imports are excluded),
for every loadable class at 4_000 x 50 and 8_000 x 100, as `peak / (n_paths * (n_steps + 1) * 8)`.
The committed ceiling must cover the worst measured ratio and sit at most 6% above it.
"""

from __future__ import annotations

import tracemalloc

import pytest
from _book_fixtures import LOADABLE, book_doc, real_config, real_universe, storyline_product_doc

from exo.greeks.bump import compute_greeks
from exo.models.memory import GREEKS_PEAK_ARRAY_RATIO
from exo.portfolio.book import estimate_job_bytes, parse_book

_SIZES = [(4_000, 50), (8_000, 100)]


def _measure(name: str, n_paths: int, n_steps_per_year: int) -> float:
    cfg = real_config()
    payoff, symbol = LOADABLE[name]
    params = cfg.models.params[symbol]

    def run(n: int, nspy: int) -> None:
        compute_greeks(
            payoff, params, scheme="qe", n_steps_per_year=nspy, n_paths=n, seed=5, bumps=cfg.greeks
        )

    run(200, 50)  # warm-up outside the trace
    tracemalloc.start()
    try:
        run(n_paths, n_steps_per_year)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return peak / (n_paths * (n_steps_per_year + 1) * 8)


def test_g_mem_greeks_peak_ratio_covers_every_loadable_class_within_6_percent() -> None:
    measured = {(name, size): _measure(name, *size) for name in LOADABLE for size in _SIZES}
    worst = max(measured.values())
    over = {k: v for k, v in measured.items() if v > GREEKS_PEAK_ARRAY_RATIO}
    assert not over, (
        f"measured ratio exceeds the committed ceiling {GREEKS_PEAK_ARRAY_RATIO}: {over}"
    )
    assert 1.06 * worst >= GREEKS_PEAK_ARRAY_RATIO, (
        f"ceiling {GREEKS_PEAK_ARRAY_RATIO} is more than 6% above the worst measured {worst:.3f}"
    )


def test_load_guard_refuses_a_book_whose_estimate_exceeds_max_job_bytes() -> None:
    cfg = real_config()
    tight = cfg.model_copy(
        update={"valuation": cfg.valuation.model_copy(update={"max_job_bytes": 1_000_000})}
    )
    doc = book_doc(storyline_product_doc(), n_paths=20_000)
    with pytest.raises(ValueError) as exc:
        parse_book(doc, tight, real_universe(cfg))
    msg = str(exc.value)
    payoff = LOADABLE["autocallable"][0]
    estimate = estimate_job_bytes(payoff, 20_000, 252)  # same expiry (1.0) as the book product
    assert "EXO-SP-AC-0001" in msg
    assert str(estimate) in msg
    assert "max_job_bytes=1000000" in msg


def test_load_guard_admits_the_shipped_book_size() -> None:
    cfg = real_config()
    payoff = LOADABLE["autocallable"][0]
    est = estimate_job_bytes(payoff, 20_000, 252)
    assert est < cfg.valuation.max_job_bytes
    assert 400e6 < est < 600e6  # 12.0 * 20_000 * 253 * 8 = 486 MB: sane next to the measured 202 MB


def test_load_guard_refuses_before_any_simulation_is_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The over-budget job is refused at load, before the Greek dry run (which would itself
    allocate): `compute_greeks` must not be reached."""
    import exo.portfolio.book as book_mod

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("compute_greeks was called for an over-budget job")

    monkeypatch.setattr(book_mod, "compute_greeks", boom)
    cfg = real_config()
    tight = cfg.model_copy(
        update={"valuation": cfg.valuation.model_copy(update={"max_job_bytes": 1_000_000})}
    )
    with pytest.raises(ValueError, match="max_job_bytes"):
        parse_book(book_doc(storyline_product_doc(), n_paths=20_000), tight, real_universe(cfg))
