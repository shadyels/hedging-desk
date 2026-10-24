"""The persisted EXO product book and its strict loader (ADR-006 Amendment 9 s7).

`book.toml` is the TERM-SHEET TRUST BOUNDARY: this loader is the only place a deserialized term
sheet becomes a payoff dataclass. The payoff dataclasses themselves validate VALUES but not TYPES
(a `bool` passes for a float field, a `list` for a tuple schedule, and a frozen dataclass holding a
list is mutable), and that stays true for term sheets built in code (pytest fixtures). So every
file-sourced field is coerced here, from `dataclasses.fields` and the type hints:

- `bool` is rejected where a float is expected; `int` is accepted as a float;
- a TOML array becomes a `tuple` for tuple fields (a non-array is an error);
- NaN/Inf are rejected; enums (`Monitoring`) and `Literal`s must match exactly;
- unknown keys and missing keys are errors, and there are no loader defaults (Amendment 4 s3).
  The only absence allowed is a field typed `X | None`: TOML has no null, so absent means `None`.

A product's currency is its underlying's refdata `currency`. Products carry no currency field and
there are no quantos, so nothing needs enforcing (ADR-014 s9); a quanto would need a currency field.

Loadable types are those with an exact time-ageing (theta) and a wire home: `autocallable`,
`barrier`, `rc`, `brc` (barrier present), `bonus`, `warrant_call`/`warrant_put` (European only),
`tarf`, `ptarf`. American warrants (LSM is low-biased and `ProductLine` has no low-bias tag) and
mini futures (a static term sheet cannot age the accruing financing level, so theta is undefined,
and there is no dividend pass-through) are rejected with those reasons.

Load-time checks beyond the schema: the book code and each underlying exist in the refdata
universe and `[models]`; the underlying's class has a band and a q label; `seed` fits the proto's
uint64 and `n_paths` is even, positive and fits uint32; every product survives a full Greek dry run
at 4 paths (grid misalignment, an observation at `t <= dt`, an oversize vol bump); and the memory
guard (`GREEKS_PEAK_ARRAY_RATIO * n_paths * (n_steps + 1) * 8 <= [valuation] max_job_bytes`) holds.
"""

from __future__ import annotations

import dataclasses
import math
import tomllib
import types
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Union, cast, get_args, get_origin, get_type_hints

from exo.config import ExoConfig
from exo.greeks.bump import compute_greeks
from exo.models.memory import GREEKS_PEAK_ARRAY_RATIO
from exo.products.autocallable import Autocallable
from exo.products.barrier import BarrierOption
from exo.products.base import Monitoring
from exo.products.bonus import BonusCertificate
from exo.products.reverse_convertible import ReverseConvertible
from exo.products.tarf import Ptarf, Tarf
from exo.products.warrant import Warrant
from exo.refdata import EQUITY_LIKE_CLASSES, FX_CLASSES, Instrument, Universe

LoadablePayoff = (
    Autocallable | BarrierOption | ReverseConvertible | BonusCertificate | Warrant | Tarf | Ptarf
)

_SEED_LIMIT = 2**64
_N_PATHS_LIMIT = 2**32
_DRY_RUN_PATHS = 4

# wire product_type -> (payoff class, fields fixed by the type itself)
_TYPES: dict[str, tuple[type, dict[str, object]]] = {
    "autocallable": (Autocallable, {}),
    "barrier": (BarrierOption, {}),
    "rc": (ReverseConvertible, {}),
    "brc": (ReverseConvertible, {}),
    "bonus": (BonusCertificate, {}),
    "warrant_call": (Warrant, {"option_type": "call"}),
    "warrant_put": (Warrant, {"option_type": "put"}),
    "tarf": (Tarf, {}),
    "ptarf": (Ptarf, {}),
}
_REJECTED_TYPES = {
    "mini_long": "mini futures are not loadable: a static term sheet cannot age the accruing "
    "financing level, so theta is undefined, and there is no dividend pass-through",
    "mini_short": "mini futures are not loadable: a static term sheet cannot age the accruing "
    "financing level, so theta is undefined, and there is no dividend pass-through",
}
_NON_PAYOFF_KEYS = {"product_id", "type", "desk_quantity"}
_BOOK_KEYS = {"code", "seed", "n_paths", "product"}


@dataclass(frozen=True)
class BookProduct:
    product_id: str
    product_type: str  # wire name: "autocallable" | "rc" | "brc" | ...
    desk_quantity: float  # negative when the desk issued the product (ADR-014 s9)
    payoff: LoadablePayoff
    instrument: Instrument  # the underlying


@dataclass(frozen=True)
class Book:
    book_id: int
    code: str
    seed: int
    n_paths: int
    products: tuple[BookProduct, ...]


def estimate_job_bytes(payoff: LoadablePayoff, n_paths: int, n_steps_per_year: int) -> int:
    """Peak bytes of one Greek job: the MEASURED ratio times one `(n_paths, n_steps + 1)` array."""
    n_steps = round(payoff.expiry * n_steps_per_year)
    return math.ceil(GREEKS_PEAK_ARRAY_RATIO * n_paths * (n_steps + 1) * 8)


def _coerce(value: object, hint: object, where: str) -> object:
    origin = get_origin(hint)
    if origin is Union or origin is types.UnionType:
        inner = [a for a in get_args(hint) if a is not type(None)]
        if len(inner) != 1:
            raise TypeError(f"{where}: unsupported union type {hint}")
        return _coerce(value, inner[0], where)
    if origin is Literal:
        if not isinstance(value, str) or value not in get_args(hint):
            raise ValueError(f"{where}: must be one of {get_args(hint)}, got {value!r}")
        return value
    if origin is tuple:
        if not isinstance(value, list):
            raise ValueError(f"{where}: must be an array, got {value!r}")
        return tuple(_coerce(v, get_args(hint)[0], f"{where}[{i}]") for i, v in enumerate(value))
    if hint is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{where}: must be a number (not bool), got {value!r}")
        if not math.isfinite(value):
            raise ValueError(f"{where}: must be finite, got {value!r}")
        return float(value)
    if hint is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{where}: must be a bool, got {value!r}")
        return value
    if hint is str:
        if not isinstance(value, str):
            raise ValueError(f"{where}: must be a string, got {value!r}")
        return value
    if isinstance(hint, type) and issubclass(hint, Monitoring):
        if not isinstance(value, str):
            raise ValueError(f"{where}: must be a string, got {value!r}")
        try:
            return hint(value)
        except ValueError:
            raise ValueError(
                f"{where}: must be one of {[m.value for m in hint]}, got {value!r}"
            ) from None
    raise TypeError(f"{where}: loader has no coercion for type {hint}")


def _is_optional(hint: object) -> bool:
    return (get_origin(hint) in (Union, types.UnionType)) and type(None) in get_args(hint)


def _build_payoff(raw: Mapping[str, object], product_type: str, where: str) -> LoadablePayoff:
    cls, fixed = _TYPES[product_type]
    hints = get_type_hints(cls)
    fields = {f.name for f in dataclasses.fields(cls)}
    given = set(raw) - _NON_PAYOFF_KEYS
    unknown = given - (fields - set(fixed))
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {sorted(unknown)} for type {product_type!r}")
    kwargs: dict[str, object] = dict(fixed)
    for name in sorted(fields - set(fixed)):
        if name in raw:
            kwargs[name] = _coerce(raw[name], hints[name], f"{where}.{name}")
        elif _is_optional(hints[name]):
            kwargs[name] = None
        else:
            raise ValueError(f"{where}: missing key {name!r} for type {product_type!r}")
    try:
        payoff = cls(**kwargs)
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from exc
    return cast(LoadablePayoff, payoff)


def _parse_product(raw: Mapping[str, object], cfg: ExoConfig, universe: Universe) -> BookProduct:
    pid = raw.get("product_id")
    if not isinstance(pid, str) or not pid:
        raise ValueError(f"product without a string product_id: {raw!r}")
    where = f"product {pid!r}"
    ptype = raw.get("type")
    if not isinstance(ptype, str):
        raise ValueError(f"{where}: missing string 'type'")
    if ptype in _REJECTED_TYPES:
        raise ValueError(f"{where}: {_REJECTED_TYPES[ptype]}")
    if ptype not in _TYPES:
        raise ValueError(f"{where}: type {ptype!r} is not loadable (allowed: {sorted(_TYPES)})")
    for key in ("desk_quantity", "underlying"):
        if key not in raw:
            raise ValueError(f"{where}: missing key {key!r}")
    desk_quantity = _coerce(raw["desk_quantity"], float, f"{where}.desk_quantity")
    if not isinstance(desk_quantity, float):  # _coerce(float) guarantees this; explicit, not assert
        raise TypeError(f"{where}.desk_quantity: coercion returned {type(desk_quantity)}")
    if desk_quantity == 0.0:
        raise ValueError(f"{where}: desk_quantity must be non-zero")
    if ptype in ("warrant_call", "warrant_put") and raw.get("exercise") == "american":
        raise ValueError(
            f"{where}: American warrants are not loadable: LSM is low-biased and "
            "ProductLine has no low-bias tag"
        )
    payoff = _build_payoff(raw, ptype, where)
    if isinstance(payoff, ReverseConvertible) and (payoff.barrier is None) != (ptype == "rc"):
        raise ValueError(
            f"{where}: type {ptype!r} needs barrier {'absent' if ptype == 'rc' else 'set'}"
        )
    symbol = payoff.underlying
    instrument = universe.instrument_by_symbol(symbol)
    if instrument is None:
        raise ValueError(f"{where}: underlying {symbol!r} is not in the refdata universe")
    if symbol not in cfg.models.params:
        raise ValueError(f"{where}: underlying {symbol!r} has no [models.{symbol}] in exo.toml")
    if instrument.instrument_class not in EQUITY_LIKE_CLASSES | FX_CLASSES:
        raise ValueError(
            f"{where}: underlying {symbol!r} has class {instrument.instrument_class}, which has "
            "no band and no q label"
        )
    return BookProduct(
        product_id=pid,
        product_type=ptype,
        desk_quantity=desk_quantity,
        payoff=payoff,
        instrument=instrument,
    )


def _check_runnable(product: BookProduct, cfg: ExoConfig, n_paths: int, seed: int) -> None:
    where = f"product {product.product_id!r}"
    estimate = estimate_job_bytes(product.payoff, n_paths, cfg.models.defaults.n_steps_per_year)
    if estimate > cfg.valuation.max_job_bytes:
        raise ValueError(
            f"{where}: Greek job estimate {estimate} bytes exceeds [valuation] max_job_bytes="
            f"{cfg.valuation.max_job_bytes} ({GREEKS_PEAK_ARRAY_RATIO} x {n_paths} paths x "
            f"{round(product.payoff.expiry * cfg.models.defaults.n_steps_per_year) + 1} steps x 8)"
        )
    try:
        compute_greeks(
            product.payoff,
            cfg.models.params[product.payoff.underlying],
            scheme=cfg.models.defaults.scheme,
            n_steps_per_year=cfg.models.defaults.n_steps_per_year,
            n_paths=_DRY_RUN_PATHS,
            seed=seed,
            bumps=cfg.greeks,
        )
    except ValueError as exc:
        raise ValueError(f"{where}: Greek dry run failed: {exc}") from exc


def parse_book(doc: Mapping[str, Any], cfg: ExoConfig, universe: Universe) -> tuple[Book, ...]:
    """Validate a parsed `book.toml` document. See the module docstring."""
    extra = set(doc) - {"book"}
    if extra or not isinstance(doc.get("book"), list) or not doc["book"]:
        raise ValueError("book.toml must contain only a non-empty [[book]] array")
    books: list[Book] = []
    seen_codes: set[str] = set()
    seen_products: set[str] = set()
    for raw_book in doc["book"]:
        if set(raw_book) != _BOOK_KEYS:
            raise ValueError(
                f"[[book]] keys must be exactly {sorted(_BOOK_KEYS)}, got {sorted(raw_book)}"
            )
        code = raw_book["code"]
        ref = universe.book_by_code(code) if isinstance(code, str) else None
        if ref is None:
            raise ValueError(f"[[book]] code {code!r} is not in the refdata universe")
        if code in seen_codes:
            raise ValueError(f"duplicate [[book]] code {code!r}")
        seen_codes.add(code)
        seed, n_paths = raw_book["seed"], raw_book["n_paths"]
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < _SEED_LIMIT:
            raise ValueError(f"book {code}: seed must be an int with 0 <= seed < 2**64")
        if (
            isinstance(n_paths, bool)
            or not isinstance(n_paths, int)
            or n_paths < _DRY_RUN_PATHS  # >= 2 antithetic pairs, or mc_estimate cannot form an SE
            or n_paths % 2 != 0
            or n_paths >= _N_PATHS_LIMIT
        ):
            raise ValueError(f"book {code}: n_paths must be an even int in [4, 2**32)")
        raw_products = raw_book["product"]
        if not isinstance(raw_products, list) or not raw_products:
            raise ValueError(f"book {code}: needs at least one [[book.product]]")
        products = tuple(_parse_product(rp, cfg, universe) for rp in raw_products)
        for p in products:
            if p.product_id in seen_products:
                raise ValueError(f"duplicate product_id {p.product_id!r}")
            seen_products.add(p.product_id)
            _check_runnable(p, cfg, n_paths, seed)
        books.append(
            Book(book_id=ref.book_id, code=code, seed=seed, n_paths=n_paths, products=products)
        )
    return tuple(books)


def load_book(path: Path, cfg: ExoConfig, universe: Universe) -> tuple[Book, ...]:
    """Read and strictly validate `book.toml` (see the module docstring)."""
    return parse_book(tomllib.loads(path.read_text()), cfg, universe)
