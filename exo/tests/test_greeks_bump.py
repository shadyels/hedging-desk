"""Unit tests for `greeks/bump.py`: ageing table, `DropLeadingStepsSource`, input guards."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

import numpy as np
import pytest
from _book_fixtures import LOADABLE, real_config

from exo.greeks.bump import (
    AGEING_NON_TIME_FIELDS,
    AGEING_TIME_FIELDS,
    BumpConfig,
    DropLeadingStepsSource,
    age_payoff,
    compute_greeks,
)
from exo.models.rng import PseudoRandomSource, SobolRandomSource
from exo.portfolio.book import _TYPES
from exo.products.autocallable import Autocallable
from exo.products.warrant import Warrant


def test_ageing_table_covers_every_dataclass_field_of_every_loadable_class() -> None:
    """A new time field must be put in the time table or the explicit non-time list: otherwise it
    would silently not age and theta would be wrong with nothing failing."""
    for cls in {cls for cls, _ in _TYPES.values()}:
        fields = {f.name for f in dataclasses.fields(cls)}
        time_fields = set(AGEING_TIME_FIELDS[cls])
        non_time = set(AGEING_NON_TIME_FIELDS[cls])
        assert not time_fields & non_time, f"{cls.__name__}: field in both lists"
        assert fields == time_fields | non_time, (
            f"{cls.__name__}: unclassified {fields - time_fields - non_time}, "
            f"stale {(time_fields | non_time) - fields}"
        )


def test_every_fixture_class_is_in_the_ageing_table() -> None:
    for payoff, _ in LOADABLE.values():
        assert type(payoff) in AGEING_TIME_FIELDS


def test_age_payoff_shifts_autocallable_times_by_dt() -> None:
    ac = LOADABLE["autocallable"][0]
    assert isinstance(ac, Autocallable)
    dt = 1 / 252
    aged = age_payoff(ac, dt)
    assert aged.observations == tuple(t - dt for t in ac.observations)
    assert aged.expiry == ac.expiry - dt
    assert aged.notional == ac.notional and aged.autocall_trigger == ac.autocall_trigger


def test_age_payoff_leaves_none_schedules_alone_and_shifts_every_time_field() -> None:
    for name, (payoff, _) in LOADABLE.items():
        aged = age_payoff(payoff, 0.01)
        assert aged.expiry == pytest.approx(payoff.expiry - 0.01), name
        for field in AGEING_TIME_FIELDS[type(payoff)]:
            before, after = getattr(payoff, field), getattr(aged, field)
            if before is None:
                assert after is None, (name, field)
            elif isinstance(before, tuple):
                assert after == tuple(t - 0.01 for t in before), (name, field)


def test_age_payoff_rejects_an_observation_at_or_before_dt() -> None:
    ac = Autocallable(**{**LOADABLE["autocallable"][0].__dict__, "observations": (0.01, 1.0)})
    with pytest.raises(ValueError, match="observations"):
        age_payoff(ac, 0.01)


def test_age_payoff_rejects_a_class_missing_from_the_table() -> None:
    @dataclass(frozen=True)
    class Mystery:
        expiry: float

        def cashflows(self, bundle: object, *, r: float) -> object:
            raise NotImplementedError

    with pytest.raises(ValueError, match="ageing table"):
        age_payoff(Mystery(1.0), 0.01)  # type: ignore[type-var]


def test_drop_leading_steps_source_slices_the_base_draw() -> None:
    base = PseudoRandomSource(11, antithetic=True)
    drop = DropLeadingStepsSource(base, 1)
    assert drop.antithetic and drop.seed == 11 and not drop.low_discrepancy
    np.testing.assert_array_equal(
        drop.normals((8, 5), stream="spot"), base.normals((8, 6), stream="spot")[:, 1:]
    )
    np.testing.assert_array_equal(
        drop.uniforms((8, 5), stream="variance"), base.uniforms((8, 6), stream="variance")[:, 1:]
    )


def test_drop_leading_steps_source_rejects_a_low_discrepancy_base() -> None:
    sobol = SobolRandomSource(seed=1, n_paths=8, dims=(("variance", 3), ("spot", 3)), replicate=0)
    with pytest.raises(ValueError, match="column-sliceable"):
        DropLeadingStepsSource(sobol, 1)
    with pytest.raises(ValueError, match="k must be"):
        DropLeadingStepsSource(PseudoRandomSource(1), 0)


def _kwargs(**over: object) -> dict[str, object]:
    cfg = real_config()
    base: dict[str, object] = {
        "scheme": "qe", "n_steps_per_year": 50, "n_paths": 200, "seed": 1, "bumps": cfg.greeks,
    }  # fmt: skip
    return {**base, **over}


def test_compute_greeks_needs_at_least_two_steps() -> None:
    cfg = real_config()
    w = Warrant("AAPL", "call", 187.5, 0.01, 1.0, "european")
    with pytest.raises(ValueError, match="must be >= 2"):
        compute_greeks(w, cfg.models.params["AAPL"], **_kwargs())  # type: ignore[arg-type]


def test_compute_greeks_rejects_an_oversize_vol_bump() -> None:
    cfg = real_config()
    w = Warrant("AAPL", "call", 187.5, 1.0, 1.0, "european")
    big = cfg.greeks.model_copy(update={"bump_vol_abs": 0.25})  # sqrt(0.04) = 0.2
    with pytest.raises(ValueError, match="bump_vol_abs"):
        compute_greeks(w, cfg.models.params["AAPL"], **_kwargs(bumps=big))  # type: ignore[arg-type]


def test_compute_greeks_is_a_pure_function_of_its_seed() -> None:
    cfg = real_config()
    w = Warrant("AAPL", "call", 187.5, 1.0, 1.0, "european")
    p = cfg.models.params["AAPL"]
    a = compute_greeks(w, p, **_kwargs())  # type: ignore[arg-type]
    b = compute_greeks(w, p, **_kwargs())  # type: ignore[arg-type]
    c = compute_greeks(w, p, **_kwargs(seed=2))  # type: ignore[arg-type]
    assert a == b
    assert a.delta != c.delta


def test_bump_config_rejects_non_positive_and_nan() -> None:
    good = real_config().greeks.model_dump()
    for key in good:
        with pytest.raises(ValueError):
            BumpConfig.model_validate({**good, key: 0.0})
        with pytest.raises(ValueError):
            BumpConfig.model_validate({**good, key: float("nan")})
    with pytest.raises(ValueError):
        BumpConfig.model_validate({**good, "bump_spot_rel": 1.0})
