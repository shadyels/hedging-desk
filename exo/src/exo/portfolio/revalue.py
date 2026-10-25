"""One book, one revaluation: the picklable, pure `ProcessPoolExecutor` target.

`revalue_book(job)` takes everything it needs in the job (the book, the marks, the base
`HestonParams`, the config subsets, the git sha and the clock reading) and touches no global,
file, bus or clock, so a result is a function of its job. Products are valued SEQUENTIALLY, so
peak memory is the maximum over products, not the sum (ADR-006 Amendment 9 s7).

The `RunManifest` it returns is the per-cycle reproducibility record (Amendment 9 s5): every
underlying valued, with `s0` = that cycle's mark; `params_hash` over exactly those params; the
book's `seed` and `n_paths`; `n_steps_per_year` (schema v3). The caller writes it (or not) and
copies `to_valuation_meta(manifest)` onto the snapshot and every target of the cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from exo.greeks.bump import BumpConfig, compute_greeks
from exo.models.params import HestonParams, Scheme
from exo.portfolio.aggregate import LineValues, hedge_targets, line_values
from exo.portfolio.book import Book
from exo.provenance import EngineSettings, RunManifest, params_hash


@dataclass(frozen=True)
class BookJob:
    book: Book
    marks: Mapping[str, float]  # underlying symbol -> mark mid (becomes that underlying's s0)
    params: Mapping[str, HestonParams]  # base parameters per underlying (their s0 is replaced)
    scheme: Scheme
    n_steps_per_year: int
    bumps: BumpConfig
    git_sha: str
    created_ns: int  # epoch ns of the cycle start; supplied by the caller so this stays pure


@dataclass(frozen=True)
class BookResult:
    lines: tuple[LineValues, ...]
    targets: dict[int, int]  # instrument_id -> target_qty_e2
    manifest: RunManifest


def model_id(scheme: str) -> str:
    return f"heston-{scheme}-pseudo-antithetic-v1"


def revalue_book(job: BookJob) -> BookResult:
    book = job.book
    symbols = sorted({p.instrument.symbol for p in book.products})
    missing = [s for s in symbols if s not in job.marks]
    if missing:
        raise ValueError(f"book {book.code}: no mark for {missing}")
    marked = {
        s: HestonParams.model_validate({**job.params[s].model_dump(), "s0": job.marks[s]})
        for s in symbols
    }
    lines: list[LineValues] = []
    for product in book.products:
        symbol = product.instrument.symbol
        greeks = compute_greeks(
            product.payoff,
            marked[symbol],
            scheme=job.scheme,
            n_steps_per_year=job.n_steps_per_year,
            n_paths=book.n_paths,
            seed=book.seed,
            bumps=job.bumps,
        )
        lines.append(
            line_values(
                product,
                greeks,
                spot=marked[symbol].s0,
                max_rel_std_error=job.bumps.max_rel_std_error,
            )
        )
    params_doc = {s: marked[s].model_dump() for s in symbols}
    stamp = datetime.fromtimestamp(job.created_ns / 1e9, tz=UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    manifest = RunManifest(
        schema_version=3,
        run_id=f"{stamp}-{book.code}-{job.created_ns % 10**9:09d}",
        created_ns=job.created_ns,
        git_sha=job.git_sha,
        model_id=model_id(job.scheme),
        params_hash=params_hash(params_doc),
        seed=book.seed,
        n_paths=book.n_paths,
        engine=EngineSettings(
            scheme=job.scheme,
            n_steps=0,
            antithetic=True,
            n_steps_per_year=job.n_steps_per_year,
        ),
        params=params_doc,
    )
    return BookResult(lines=tuple(lines), targets=hedge_targets(lines), manifest=manifest)
