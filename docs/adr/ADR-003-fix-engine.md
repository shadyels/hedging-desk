# ADR-003: FIX 4.4 via the `quickfix` Rust crate; venue dialects deferred

**Status:** Accepted **Date:** 2026-07-05 **Deciders:** desk lead (FIX 4.4 + QuickFIX confirmed)

## Context

Delta One emits orders over FIX 4.4. Demo counterparty is `sim/`'s acceptor. Rust FIX options surveyed:

- **`quickfix` crate** — high-level binding to the C++ QuickFIX library; supports FIX 4.x with spec-driven validation, code-generated type-safe messages, session state stores, and describes itself as ready for production/real application use, with the caveat that the API may still change (arthurlm, n.d., https://github.com/arthurlm/quickfix-rs; crates.io, 2026-02-11, https://crates.io/crates/quickfix).
- **Pure-Rust engines** — `ferrumfix`/`fefix` (layered FIX stack incl. session layer; ferrumfix, n.d., https://github.com/ferrumfix/ferrumfix) and its continuation `rustyfix` ("FIX & FAST in pure Rust"; crates.io, n.d., https://crates.io/crates/rustyfix). Attractive (no C++ toolchain, no FFI) but session-layer maturity vs battle-tested QuickFIX is the open question, and the user explicitly accepted QuickFIX.

## Decision

`quickfix` crate (QuickFIX C++ binding) in `d1-gateway-fix`, FIX 4.4, messages: 35=D, 8, F, G. The C++/FFI surface is confined to that crate, off the hot path, exempted (only there) from the workspace `unsafe` ban via the crate's own boundary — application code stays safe Rust.

Venue dialects (Bloomberg EMSX, TSOX, FXAll) are **adapters, not now**: their FIX dialects are proprietary specifications requiring firm entitlements; the gateway exposes a `VenueDialect` trait so adapters slot in without touching order logic. Demo claims "standard FIX 4.4, dialect-ready", nothing more.

## Consequences

- Easier: spec-correct session handling (sequence, resend, logon) for free; same engine family as `sim/`'s acceptor → honest session-level testing.
- Harder: C++ build dependency in CI; pinned crate version until its API stabilizes. Pure-Rust `rustyfix` is the named successor path if the FFI build burden ever outweighs its value (would supersede this ADR).
