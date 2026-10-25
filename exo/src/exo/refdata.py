"""Read-only view of `protocol/refdata/universe.json`: books and instruments.

A plain JSON read, validated for the fields EXO consumes (ids, symbol, class, currency, lot size)
and nothing else. Market conventions (tick sizes, settlement lags, ...) are deliberately not
exposed: they are defined once in `protocol/` and EXO has no use for them (root CLAUDE.md).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# Instrument classes the proto's `InstrumentClass` enum can carry; refdata spells them without the
# `INSTRUMENT_CLASS_` prefix.
_KNOWN_CLASSES = frozenset(
    {
        "EQUITY",
        "INDEX",
        "ETF",
        "OPTION",
        "FORWARD",
        "FUTURE",
        "FX_SPOT",
        "FX_FORWARD",
        "BOND",
    }
)


# Underlying classes a book may hold: one grouping drives both the band (`[rehedge]`) and the q
# label (dividend sensitivity vs foreign rho) in `portfolio/aggregate.py`; anything else is a load
# error (ADR-014 s9, ADR-006 Amendment 9 s4).
EQUITY_LIKE_CLASSES = frozenset({"EQUITY", "INDEX", "ETF"})
FX_CLASSES = frozenset({"FX_SPOT"})


@dataclass(frozen=True)
class BookRef:
    book_id: int
    code: str


@dataclass(frozen=True)
class Instrument:
    instrument_id: int
    symbol: str
    instrument_class: str  # "EQUITY" | "INDEX" | "ETF" | "FX_SPOT" | ... (refdata spelling)
    currency: str
    lot_e2: int


@dataclass(frozen=True)
class Universe:
    books: tuple[BookRef, ...]
    instruments: tuple[Instrument, ...]

    def book_by_code(self, code: str) -> BookRef | None:
        return next((b for b in self.books if b.code == code), None)

    def instrument_by_symbol(self, symbol: str) -> Instrument | None:
        return next((i for i in self.instruments if i.symbol == symbol), None)

    def instrument_by_id(self, instrument_id: int) -> Instrument | None:
        return next((i for i in self.instruments if i.instrument_id == instrument_id), None)


def _int(row: Mapping[str, object], key: str, where: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where}: {key!r} must be an int, got {value!r}")
    return value


def _str(row: Mapping[str, object], key: str, where: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: {key!r} must be a non-empty string, got {value!r}")
    return value


def load_universe(path: Path) -> Universe:
    """Parse `universe.json`; raises `ValueError` on a malformed or duplicate-keyed row."""
    doc = json.loads(path.read_text())
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: universe must be a JSON object")
    for key in ("books", "instruments"):
        rows = doc.get(key)
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ValueError(f"{path}: {key!r} must be an array of objects")
    books: list[BookRef] = []
    for row in doc["books"]:
        where = f"universe book {row!r}"
        books.append(BookRef(book_id=_int(row, "book_id", where), code=_str(row, "code", where)))
    instruments: list[Instrument] = []
    for row in doc["instruments"]:
        where = f"universe instrument {row.get('symbol')!r}"
        cls = _str(row, "class", where)
        if cls not in _KNOWN_CLASSES:
            raise ValueError(f"{where}: unknown instrument class {cls!r}")
        lot = _int(row, "lot_e2", where)
        if lot <= 0:
            raise ValueError(f"{where}: lot_e2 must be positive, got {lot}")
        instruments.append(
            Instrument(
                instrument_id=_int(row, "instrument_id", where),
                symbol=_str(row, "symbol", where),
                instrument_class=cls,
                currency=_str(row, "currency", where),
                lot_e2=lot,
            )
        )
    for label, keys in (
        ("book_id", [b.book_id for b in books]),
        ("book code", [b.code for b in books]),
        ("instrument_id", [i.instrument_id for i in instruments]),
        ("instrument symbol", [i.symbol for i in instruments]),
    ):
        if len(set(keys)) != len(keys):
            raise ValueError(f"universe has a duplicate {label}")
    return Universe(books=tuple(books), instruments=tuple(instruments))
