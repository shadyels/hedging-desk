//! Core <-> gateway DTOs for internal crosses and directed transfers (P1.M3
//! Slice 3). Plain structs only -- ADR-004 keeps proto out of `d1-core`;
//! `d1-gateway-nats::convert` does the proto conversion at the edge, exactly
//! like `d1-core::order::ExecReport` / `d1-core::target::Target` already do
//! for exec reports and EXO targets.

use crate::ids::{BookId, InstrumentId};

/// Core -> gateway DTO for one booked internal cross leg pair (netting- or
/// transfer-derived, ADR-005/ADR-009). Minted by
/// `crates/d1/src/cycle.rs::book_cross` at booking time -- `cross_id` is the
/// compliance-lineage key that must match on both the NATS `d1.crosses`
/// notice and (P1.M4) the Kafka `posttrade.crosses` record keyed by
/// `cross_id`. Mirrors `ExecReport`'s role for the exec-report path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CrossRecord {
    /// Compliance-lineage identity for this cross, minted once at booking
    /// time (UUIDv7). Distinct from `Meta.msg_id`, which is minted fresh at
    /// the NATS edge for wire dedupe (root CLAUDE.md invariant #4).
    pub cross_id: uuid::Uuid,
    /// Instrument this cross trades.
    pub instrument: InstrumentId,
    /// Book on the buy side of this cross.
    pub buy_book: BookId,
    /// Book on the sell side of this cross.
    pub sell_book: BookId,
    /// Quantity crossed, fixed-point x10^2. Always `> 0`.
    pub qty_e2: i64,
    /// Cross reference price, fixed-point x10^9.
    pub ref_px_e9: i64,
    /// `RefPxPolicy::as_str()` that produced `ref_px_e9`, stamped for
    /// compliance lineage (ADR-005 §4).
    pub policy_id: &'static str,
}

/// Gateway -> core DTO for an inbound directed internal transfer (ADR-009):
/// instructs Delta One to book a cross between two books by instruction, not
/// via netting-cycle demand offsetting. Consumed by
/// `crates/d1/src/cycle.rs::NettingSession::on_transfer`.
///
/// ponytail: deliberately omits `transfer_id`/`reason` -- those are M4 Kafka
/// lineage fields, not needed to book the cross itself; inbound dedupe is on
/// `Meta.msg_id` at the gateway (root CLAUDE.md invariant #4), same as every
/// other inbound message. Business-level transfer idempotency (keyed on
/// `transfer_id`) is deferred to M4 lineage, same deferral noted on
/// `live.proto`'s `InternalTransferRequest.transfer_id`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TransferRequest {
    /// Instrument being transferred.
    pub instrument: InstrumentId,
    /// Book shedding the risk (sells).
    pub from_book: BookId,
    /// Book receiving the risk (buys).
    pub to_book: BookId,
    /// Quantity transferred, fixed-point x10^2. Must be `> 0`; validated by
    /// the caller (`crates/d1/src/lib.rs::run_core`), not this type.
    pub qty_e2: i64,
}
