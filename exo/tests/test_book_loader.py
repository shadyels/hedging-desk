"""The persisted book's strict loader (ADR-006 Amendment 9 s7): the term-sheet trust boundary."""

from __future__ import annotations

from typing import Any

import pytest
from _book_fixtures import (
    BOOK_TOML,
    book_doc,
    real_config,
    real_universe,
    storyline_product_doc,
)

from exo.portfolio.book import Book, load_book, parse_book
from exo.products.autocallable import Autocallable
from exo.products.base import Monitoring
from exo.products.tarf import Ptarf, Tarf
from exo.products.warrant import Warrant

CFG = real_config()
UNI = real_universe(CFG)


def _load(*products: dict[str, Any], seed: int = 7, n_paths: int = 1000) -> tuple[Book, ...]:
    return parse_book(book_doc(*products, seed=seed, n_paths=n_paths), CFG, UNI)


def _rejects(match: str, *products: dict[str, Any], seed: int = 7, n_paths: int = 1000) -> None:
    with pytest.raises(ValueError, match=match):
        _load(*products, seed=seed, n_paths=n_paths)


def test_shipped_book_loads_with_book_id_and_product_id() -> None:
    (book,) = load_book(BOOK_TOML, CFG, UNI)
    assert (book.book_id, book.code, book.seed, book.n_paths) == (3, "EXO-SP", 20261024, 20_000)
    (p,) = book.products
    assert p.product_id == "EXO-SP-AC-0001" and p.product_type == "autocallable"
    assert p.desk_quantity == -1.0
    assert p.instrument.instrument_id == 1001 and p.instrument.currency == "USD"
    assert isinstance(p.payoff, Autocallable)
    assert p.payoff.observations == (0.25, 0.5, 0.75, 1.0)
    assert isinstance(p.payoff.observations, tuple)  # list -> tuple


def test_int_is_accepted_for_float() -> None:
    (book,) = _load(storyline_product_doc(notional=10_000_000, coupon_rate=0))
    assert isinstance(book.products[0].payoff, Autocallable)
    assert book.products[0].payoff.notional == 10_000_000.0
    assert isinstance(book.products[0].payoff.notional, float)


@pytest.mark.parametrize("field", ["notional", "autocall_trigger", "expiry", "coupon_rate"])
def test_bool_for_float_is_rejected(field: str) -> None:
    _rejects("not bool", storyline_product_doc(**{field: True}))


def test_bool_for_desk_quantity_is_rejected() -> None:
    _rejects("desk_quantity", storyline_product_doc(desk_quantity=True))


def test_non_bool_for_bool_field_is_rejected() -> None:
    _rejects("must be a bool", storyline_product_doc(memory=1))


def test_non_list_for_tuple_field_is_rejected() -> None:
    _rejects("must be an array", storyline_product_doc(observations=0.25))
    _rejects("not bool", storyline_product_doc(observations=[0.25, True, 1.0]))


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_numbers_are_rejected(bad: float) -> None:
    _rejects("finite", storyline_product_doc(notional=bad))


def test_unknown_key_is_rejected() -> None:
    _rejects("unknown key", storyline_product_doc(extra_field=1.0))


def test_missing_key_is_rejected_and_there_are_no_defaults() -> None:
    doc = storyline_product_doc()
    del doc["coupon_rate"]
    _rejects("missing key 'coupon_rate'", doc)
    doc = storyline_product_doc()
    del doc["memory"]
    _rejects("missing key 'memory'", doc)
    doc = storyline_product_doc()
    del doc["desk_quantity"]
    _rejects("desk_quantity", doc)


def test_optional_fields_may_be_absent() -> None:
    barrier = {
        "product_id": "B1", "type": "barrier", "desk_quantity": 1.0, "underlying": "AAPL",
        "option_type": "call", "strike": 187.5, "expiry": 1.0, "barrier": 150.0,
        "direction": "down", "knock": "out", "monitoring": "continuous_bridge",
    }  # fmt: skip
    (book,) = _load(barrier)
    assert book.products[0].payoff.monitoring == Monitoring.CONTINUOUS_BRIDGE  # type: ignore[union-attr]
    assert book.products[0].payoff.observations is None  # type: ignore[union-attr]


def test_bad_enum_and_literal_values_are_rejected() -> None:
    barrier = {
        "product_id": "B1", "type": "barrier", "desk_quantity": 1.0, "underlying": "AAPL",
        "option_type": "call", "strike": 187.5, "expiry": 1.0, "barrier": 150.0,
        "direction": "down", "knock": "out", "monitoring": "continuous_bridge",
    }  # fmt: skip
    _rejects("monitoring", {**barrier, "monitoring": "CONTINUOUS"})
    _rejects("direction", {**barrier, "direction": "sideways"})


def test_unknown_underlying_and_unmodelled_underlying_are_rejected() -> None:
    _rejects("not in the refdata universe", storyline_product_doc(underlying="APPL"))
    no_msft = CFG.model_copy(
        update={
            "models": CFG.models.model_copy(
                update={"params": {k: v for k, v in CFG.models.params.items() if k != "MSFT"}}
            )
        }
    )
    with pytest.raises(ValueError, match=r"no \[models.MSFT\]"):
        parse_book(book_doc(storyline_product_doc(underlying="MSFT")), no_msft, UNI)


def test_underlying_class_without_a_band_or_q_label_is_rejected() -> None:
    with_future = CFG.model_copy(
        update={
            "models": CFG.models.model_copy(
                update={"params": {**CFG.models.params, "ESZ6": CFG.models.params["SPX"]}}
            )
        }
    )
    with pytest.raises(ValueError, match="no band and no q label"):
        parse_book(book_doc(storyline_product_doc(underlying="ESZ6")), with_future, UNI)


def test_american_warrant_is_rejected_with_its_reason() -> None:
    warrant = {
        "product_id": "W1", "type": "warrant_call", "desk_quantity": -100.0, "underlying": "AAPL",
        "strike": 187.5, "expiry": 1.0, "ratio": 1.0, "exercise": "american",
    }  # fmt: skip
    _rejects("American warrants are not loadable.*low-bias", warrant)


def test_european_warrant_type_fixes_option_type_and_rejects_a_contradicting_key() -> None:
    warrant = {
        "product_id": "W1", "type": "warrant_put", "desk_quantity": -100.0, "underlying": "AAPL",
        "strike": 187.5, "expiry": 1.0, "ratio": 1.0, "exercise": "european",
    }  # fmt: skip
    (book,) = _load(warrant)
    payoff = book.products[0].payoff
    assert isinstance(payoff, Warrant) and payoff.option_type == "put"
    _rejects("unknown key", {**warrant, "option_type": "put"})


@pytest.mark.parametrize("kind", ["mini_long", "mini_short"])
def test_mini_futures_are_rejected_with_their_reason(kind: str) -> None:
    _rejects("mini futures are not loadable.*theta is undefined", storyline_product_doc(type=kind))


def test_type_outside_the_allow_list_is_rejected() -> None:
    _rejects("not loadable", storyline_product_doc(type="snowball"))
    _rejects(
        "missing string 'type'", {k: v for k, v in storyline_product_doc().items() if k != "type"}
    )


def test_rc_versus_brc_is_checked_against_the_barrier() -> None:
    rc = {
        "product_id": "R1", "type": "rc", "desk_quantity": -1.0, "underlying": "AAPL",
        "notional": 1e6, "strike": 187.5, "coupon_rate": 0.02, "coupon_times": [0.5, 1.0],
        "expiry": 1.0,
    }  # fmt: skip
    _load(rc)
    brc = {**rc, "type": "brc", "barrier": 131.25, "monitoring": "continuous_bridge"}
    _load(brc)
    _rejects("needs barrier absent", {**rc, "barrier": 131.25, "monitoring": "continuous_bridge"})
    _rejects("needs barrier set", {**rc, "type": "brc"})


def test_seed_range_and_n_paths_checks() -> None:
    p = storyline_product_doc()
    _rejects("seed", p, seed=2**64)
    _rejects("seed", p, seed=-1)
    _rejects("seed", p, seed=True)
    _load(p, seed=2**64 - 1)
    _rejects("n_paths", p, n_paths=999)
    _rejects("n_paths", p, n_paths=0)
    _rejects("n_paths", p, n_paths=2)  # < 2 antithetic pairs: mc_estimate cannot form an SE
    _load(p, n_paths=4)
    _rejects("n_paths", p, n_paths=2**32)


def test_desk_quantity_must_be_nonzero() -> None:
    _rejects("non-zero", storyline_product_doc(desk_quantity=0.0))


def test_duplicate_product_id_and_unknown_book_code_are_rejected() -> None:
    _rejects("duplicate product_id", storyline_product_doc(), storyline_product_doc())
    doc = book_doc(storyline_product_doc())
    doc["book"][0]["code"] = "NOPE"
    with pytest.raises(ValueError, match="not in the refdata universe"):
        parse_book(doc, CFG, UNI)
    doc["book"][0]["code"] = "EXO-SP"
    doc["book"].append(dict(doc["book"][0]))
    with pytest.raises(ValueError, match="duplicate \\[\\[book\\]\\] code"):
        parse_book(doc, CFG, UNI)


def test_book_level_keys_are_exact() -> None:
    doc = book_doc(storyline_product_doc())
    doc["book"][0]["extra"] = 1
    with pytest.raises(ValueError, match="keys must be exactly"):
        parse_book(doc, CFG, UNI)
    with pytest.raises(ValueError, match="non-empty"):
        parse_book({}, CFG, UNI)


def test_dry_run_rejects_an_observation_at_dt() -> None:
    """An observation at 1/252 ages to t = 0, which the term sheet rejects: the load-time Greek
    dry run turns that into a load error instead of a failure on the first live revaluation."""
    _rejects("dry run failed", storyline_product_doc(observations=[1 / 252, 0.5, 1.0]))


def test_dry_run_rejects_an_observation_off_the_grid() -> None:
    _rejects("dry run failed.*grid", storyline_product_doc(observations=[0.2501, 0.5, 1.0]))


def test_term_sheet_value_errors_name_the_product() -> None:
    _rejects("EXO-SP-AC-0001.*notional", storyline_product_doc(notional=-1.0))


def test_fx_underlying_tarf_and_ptarf_are_loadable_with_book_and_product_ids() -> None:
    """Amendment 8/9: FX_SPOT underlyings load (the shipped storyline book is equity-only)."""
    tarf = {
        "product_id": "T1", "type": "tarf", "desk_quantity": -1.0, "underlying": "EURUSD",
        "notional": 1e6, "strike": 1.08, "leverage": 2.0, "target": 60_000.0,
        "fixings": [0.25, 0.5, 0.75, 1.0], "expiry": 1.0,
    }  # fmt: skip
    ptarf = {
        "product_id": "T2", "type": "ptarf", "desk_quantity": 1.0, "underlying": "EURUSD",
        "notional": 1e6, "strike_low": 1.05, "pivot": 1.08, "strike_high": 1.11,
        "leverage": 2.0, "target": 60_000.0, "fixings": [0.25, 0.5, 0.75, 1.0], "expiry": 1.0,
    }  # fmt: skip
    (book,) = _load(tarf, ptarf)
    assert book.book_id == 3
    assert [p.product_id for p in book.products] == ["T1", "T2"]
    assert all(p.instrument.instrument_class == "FX_SPOT" for p in book.products)
    assert isinstance(book.products[0].payoff, Tarf) and isinstance(book.products[1].payoff, Ptarf)
    assert book.products[0].desk_quantity == -1.0


def test_shipped_book_is_the_exo_sp_autocallable_only() -> None:
    (book,) = load_book(BOOK_TOML, CFG, UNI)
    assert [type(p.payoff) for p in book.products] == [Autocallable]
    assert {p.instrument.instrument_class for p in book.products} == {"EQUITY"}
