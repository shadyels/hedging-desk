//! EXO target value type, consumed by `d1::cycle::NettingSession` (P1.M3
//! Slice 2). Plain core type only -- ADR-004: the proto `TargetPosition`
//! never crosses into `d1-core`; `d1-gateway-nats::convert` does that
//! conversion at the edge, exactly like `d1-gateway-fix::convert` does for
//! FIX.

use crate::ids::{BookId, InstrumentId};

/// A desired absolute position for one (book, instrument), as published by
/// EXO on `exo.targets.<book>.<instrument>` (`protocol/nats-subjects.md`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Target {
    /// Book this target applies to.
    pub book: BookId,
    /// Instrument being targeted.
    pub instrument: InstrumentId,
    /// Desired absolute position, fixed-point x10^2 -- **absolute**, not a
    /// delta-order (root CLAUDE.md invariant #5: EXO output is a target, D1
    /// decides what, if anything, to execute).
    pub target_qty_e2: i64,
    /// No-trade band half-width for this book/instrument this cycle,
    /// fixed-point x10^2 (`TargetPosition.band_qty_e2`,
    /// `protocol/proto/live.proto` field 6). Must be `>= 0`; `d1-netting`
    /// validates this at the trust boundary.
    pub band_e2: i64,
}
