# ADR-013: `rtrb` for core-adjacent SPSC rings

**Status:** Accepted
**Date:** 2026-07-14
**Deciders:** desk lead (P1.M2 slice 2 planning)

## Context

`delta-one/CLAUDE.md`'s threading model has stood as "`rtrb` or `crossbeam` ArrayQueue — pick once, ADR it" since P1.M1: nothing needed a ring until a gateway showed up. P1.M2 slice 2 wires the first one, between the core thread and `d1-gateway-fix` (outbound orders, inbound execs), so the choice is no longer deferrable.

Both candidates are SPSC-capable bounded queues usable off a single producer/single consumer pair:

- **`rtrb`** — purpose-built lock-free SPSC ring buffer (`no_std`-capable, zero allocation after construction). `push`/`pop` are wait-free and return `Result` immediately (`PushError::Full` / `PopError::Empty`); there is no blocking variant and no disconnect/close signal — a dropped `Producer`/`Consumer` is only observable by the other side continuing to see `Full`/`Empty` forever.
- **`crossbeam-channel`** (bounded) — general MPMC channel. Its bounded flavor takes an internal lock on send/recv (parking-lot mutex + condvar), which is exactly the "no locks on the hot path" rule (`delta-one/CLAUDE.md` hot-path contract #2) this ring would violate on the core-thread end. It also carries MPMC machinery (multiple producer/consumer bookkeeping) this project never needs — every ring here is SPSC by construction (one core thread, one gateway thread per ring).

## Decision

`rtrb` for every core-adjacent SPSC ring, starting with the two in `crates/d1` (outbound `Order`, inbound `ExecEvent`) between the core thread and `d1-gateway-fix`. One crate, one discipline, so a future ring (the feed-ingest ring, Slice 3) is a known pattern, not a fresh choice.

- Core-thread end (`Consumer::pop`/`Producer::push`) is lock-free and allocation-free after the ring is constructed at startup — satisfies hot-path contract #1/#2.
- The off-path gateway-thread end parks with a short backoff (`std::thread::sleep` on `Empty`/`Full`) rather than busy-spinning; it is not latency-critical (ADR-004: the FIX socket send is already off the 10–50 µs path).
- **Shutdown is an explicit `AtomicBool` flag**, checked each poll-loop iteration on both ends, because `rtrb` has no disconnect signal to select/park on. This is not a gap specific to this ADR's choice — `crossbeam-channel`'s bounded flavor would need the same explicit flag for a clean core-thread shutdown, since the core thread must never block on a `recv()` that could hang past shutdown.

## Consequences

- Easier: no lock anywhere on the core-thread poll loop; `rtrb`'s API is small enough that the gateway drain loop and the core consume loop are both a few lines.
- Harder: every ring needs its own shutdown flag (or a shared one, if the rings are always torn down together) — there's no "channel closed" `Err` variant to propagate. Documented here so the next ring (Slice 3 feed-ingest) doesn't rediscover this.
