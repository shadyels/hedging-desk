//! `d1-core`'s error type (delta-one/CLAUDE.md: one `thiserror` enum per crate).

use crate::order::OrderStatus;

/// Failure modes for order-state-machine operations (`crate::order`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum OrderError {
    /// No order exists for the given `ClOrdId`.
    #[error("unknown order")]
    UnknownOrder,
    /// The reported status is not a legal transition from the current status.
    #[error("illegal transition from {from:?} to {to:?}")]
    IllegalTransition {
        /// Status the order was in before this exec.
        from: OrderStatus,
        /// Status the exec reported.
        to: OrderStatus,
    },
    /// A quantity or notional computation overflowed `i64`.
    #[error("quantity overflow")]
    Overflow,
    /// The order is already in a terminal state and cannot accept further execs.
    #[error("order already terminal")]
    AlreadyTerminal,
}
