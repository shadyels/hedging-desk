//! Order state machine: tracks in-flight orders and applies execution
//! events. Pure logic only — no FIX, no NATS (ADR-004: proto/bus stay out of
//! `d1-core`). This mirrors the proto `OrdStatus` vocabulary with a native
//! enum, exactly like the existing `d1-core::Side` vs proto `Side` split;
//! gateways convert wire messages to `ExecEvent` at the edge (Slice 2/3).

use std::collections::{HashMap, HashSet};

use crate::error::OrderError;
use crate::ids::{BookId, ClOrdId, ExecId, InstrumentId};
use crate::keeper::Side;

/// Lifecycle state of an order, mirroring proto `OrdStatus` (minus
/// `Unspecified`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderStatus {
    /// Accepted, no fills yet.
    New,
    /// Some but not all of `order_qty_e2` has been filled.
    PartiallyFilled,
    /// Fully filled. Terminal.
    Filled,
    /// Rejected by the counterparty/exchange. Terminal.
    Rejected,
    /// Canceled. Terminal.
    Canceled,
    /// A cancel request is in flight.
    // ponytail: not driven in Slice 1 — FIX 35=F (OrderCancelRequest) lands
    // with the FIX gateway (Slice 2).
    PendingCancel,
    /// A cancel/replace request is in flight.
    // ponytail: not driven in Slice 1 — FIX 35=G (OrderCancelReplaceRequest)
    // lands with the FIX gateway (Slice 2).
    PendingReplace,
}

impl OrderStatus {
    /// Terminal statuses accept no further execs.
    #[must_use]
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            OrderStatus::Filled | OrderStatus::Rejected | OrderStatus::Canceled
        )
    }
}

/// A tracked order.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Order {
    /// Client-assigned order id, unique per order.
    pub cl_ord_id: ClOrdId,
    /// Book this order hedges.
    pub book: BookId,
    /// Instrument being traded.
    pub instrument: InstrumentId,
    /// Buy or sell.
    pub side: Side,
    /// Original requested quantity, fixed-point ×10².
    pub order_qty_e2: i64,
    /// Limit price, fixed-point ×10⁹. Zero means market order (ManualOrder
    /// demo semantics).
    pub limit_px_e9: i64,
    /// Current lifecycle state.
    pub status: OrderStatus,
    /// Quantity filled so far, fixed-point ×10².
    pub cum_qty_e2: i64,
    /// Quantity remaining to fill, fixed-point ×10².
    pub leaves_qty_e2: i64,
    /// Price of the most recent fill, fixed-point ×10⁹. Zero if never filled.
    pub last_px_e9: i64,
}

/// An execution event: what the FIX gateway converts an `ExecutionReport`
/// into (Slice 2). Pure, no proto.
#[derive(Debug, Clone, Copy)]
pub struct ExecEvent {
    /// Which order this exec applies to.
    pub cl_ord_id: ClOrdId,
    /// Unique id for this exec, used for idempotent dedupe.
    pub exec_id: ExecId,
    /// Status the counterparty/exchange reports after this exec.
    pub reported_status: OrderStatus,
    /// Quantity filled by this specific exec (0 for a non-fill status
    /// change, e.g. a reject), fixed-point ×10².
    pub last_qty_e2: i64,
    /// Price of this specific fill, fixed-point ×10⁹.
    pub last_px_e9: i64,
}

/// What a fill event yields; the caller books it via
/// `PositionKeeper::apply_fill`. The order store stays decoupled from the
/// keeper so each is independently testable.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Fill {
    /// Book to credit/debit.
    pub book: BookId,
    /// Instrument filled.
    pub instrument: InstrumentId,
    /// Buy or sell.
    pub side: Side,
    /// Quantity filled, fixed-point ×10².
    pub qty_e2: i64,
    /// Fill price, fixed-point ×10⁹.
    pub px_e9: i64,
}

/// Preallocated order store: `ClOrdId` -> `Order`, plus exec-id dedupe for
/// idempotent replay (root CLAUDE.md invariant #4).
pub struct OrderStore {
    orders: Vec<Order>,
    index: HashMap<ClOrdId, usize>,
    seen_execs: HashSet<ExecId>,
}

impl OrderStore {
    /// Preallocate for up to `capacity` concurrently-tracked orders.
    // ponytail: append-only slab, no slot reuse for terminal orders — fine
    // while capacity is sized generously for a single session; a freelist
    // reclaiming terminal-order slots lands if/when long-uptime memory
    // bounding actually matters (Slice 2/3, once a real gateway drives this
    // continuously).
    #[must_use]
    pub fn new(capacity: usize) -> Self {
        Self {
            orders: Vec::with_capacity(capacity),
            index: HashMap::with_capacity(capacity),
            seen_execs: HashSet::with_capacity(capacity),
        }
    }

    /// Insert a new order. Forces `status = New`, `cum = 0`,
    /// `leaves = order_qty` regardless of what the caller passed in.
    pub fn place(&mut self, mut order: Order) -> ClOrdId {
        order.status = OrderStatus::New;
        order.cum_qty_e2 = 0;
        order.leaves_qty_e2 = order.order_qty_e2;
        let cl_ord_id = order.cl_ord_id;
        let slot = self.orders.len();
        self.orders.push(order);
        self.index.insert(cl_ord_id, slot);
        cl_ord_id
    }

    /// Current state of a tracked order, if any.
    #[must_use]
    pub fn get(&self, cl_ord_id: ClOrdId) -> Option<Order> {
        let &slot = self.index.get(&cl_ord_id)?;
        self.orders.get(slot).copied()
    }

    /// Apply an execution event: dedupe replayed `ExecId`s (idempotent
    /// no-op), reject execs on an unknown or already-terminal order,
    /// validate the transition, update `cum`/`leaves`/`status`, and return
    /// `Some(Fill)` when the exec carried a fill quantity.
    pub fn apply_exec(&mut self, event: &ExecEvent) -> Result<Option<Fill>, OrderError> {
        if self.seen_execs.contains(&event.exec_id) {
            return Ok(None);
        }

        let &slot = self
            .index
            .get(&event.cl_ord_id)
            .ok_or(OrderError::UnknownOrder)?;
        let order = self.orders.get_mut(slot).ok_or(OrderError::UnknownOrder)?;
        if order.status.is_terminal() {
            return Err(OrderError::AlreadyTerminal);
        }

        // Only New/PartiallyFilled ever reach here (terminal is rejected
        // above); PendingCancel/PendingReplace are unreachable in Slice 1
        // since nothing produces them yet, so they fall through to the
        // catch-all like any other genuinely illegal move.
        match (order.status, event.reported_status) {
            (OrderStatus::New | OrderStatus::PartiallyFilled, OrderStatus::PartiallyFilled)
            | (
                OrderStatus::New | OrderStatus::PartiallyFilled,
                OrderStatus::Filled | OrderStatus::Rejected | OrderStatus::Canceled,
            ) => {}
            (from, to) => return Err(OrderError::IllegalTransition { from, to }),
        }

        // Trust the exec's reported cum/leaves inputs as authoritative (the
        // FIX session already resolved them) rather than recomputing and
        // cross-checking locally.
        let new_cum = order
            .cum_qty_e2
            .checked_add(event.last_qty_e2)
            .ok_or(OrderError::Overflow)?;
        let new_leaves = order
            .leaves_qty_e2
            .checked_sub(event.last_qty_e2)
            .ok_or(OrderError::Overflow)?;

        order.cum_qty_e2 = new_cum;
        order.leaves_qty_e2 = new_leaves;
        order.status = event.reported_status;
        let fill = if event.last_qty_e2 > 0 {
            order.last_px_e9 = event.last_px_e9;
            Some(Fill {
                book: order.book,
                instrument: order.instrument,
                side: order.side,
                qty_e2: event.last_qty_e2,
                px_e9: event.last_px_e9,
            })
        } else {
            None
        };

        self.seen_execs.insert(event.exec_id);
        Ok(fill)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)] // tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn sample_order(cl_ord_id: ClOrdId) -> Order {
        Order {
            cl_ord_id,
            book: BookId(1),
            instrument: InstrumentId(1001),
            side: Side::Buy,
            order_qty_e2: 10_000,
            limit_px_e9: 0,
            status: OrderStatus::New,
            cum_qty_e2: 0,
            leaves_qty_e2: 0,
            last_px_e9: 0,
        }
    }

    #[test]
    fn place_starts_new_with_full_leaves() {
        let mut store = OrderStore::new(4);
        let id = ClOrdId::from_seq(1);
        store.place(sample_order(id));
        let order = store.get(id).unwrap();
        assert_eq!(order.status, OrderStatus::New);
        assert_eq!(order.leaves_qty_e2, 10_000);
        assert_eq!(order.cum_qty_e2, 0);
    }

    #[test]
    fn partial_then_full_fill_reaches_filled() {
        let mut store = OrderStore::new(4);
        let id = ClOrdId::from_seq(1);
        store.place(sample_order(id));

        let fill1 = store
            .apply_exec(&ExecEvent {
                cl_ord_id: id,
                exec_id: ExecId::from_bytes([1; 20]),
                reported_status: OrderStatus::PartiallyFilled,
                last_qty_e2: 4_000,
                last_px_e9: 150_000_000_000,
            })
            .unwrap();
        assert_eq!(
            fill1,
            Some(Fill {
                book: BookId(1),
                instrument: InstrumentId(1001),
                side: Side::Buy,
                qty_e2: 4_000,
                px_e9: 150_000_000_000,
            })
        );
        let order = store.get(id).unwrap();
        assert_eq!(order.status, OrderStatus::PartiallyFilled);
        assert_eq!(order.cum_qty_e2, 4_000);
        assert_eq!(order.leaves_qty_e2, 6_000);

        let fill2 = store
            .apply_exec(&ExecEvent {
                cl_ord_id: id,
                exec_id: ExecId::from_bytes([2; 20]),
                reported_status: OrderStatus::Filled,
                last_qty_e2: 6_000,
                last_px_e9: 150_500_000_000,
            })
            .unwrap();
        assert_eq!(fill2.unwrap().qty_e2, 6_000);
        let order = store.get(id).unwrap();
        assert_eq!(order.status, OrderStatus::Filled);
        assert_eq!(order.cum_qty_e2, 10_000);
        assert_eq!(order.leaves_qty_e2, 0);
    }

    #[test]
    fn reject_moves_to_terminal_with_no_fill() {
        let mut store = OrderStore::new(4);
        let id = ClOrdId::from_seq(1);
        store.place(sample_order(id));

        let fill = store
            .apply_exec(&ExecEvent {
                cl_ord_id: id,
                exec_id: ExecId::from_bytes([1; 20]),
                reported_status: OrderStatus::Rejected,
                last_qty_e2: 0,
                last_px_e9: 0,
            })
            .unwrap();
        assert_eq!(fill, None);
        assert_eq!(store.get(id).unwrap().status, OrderStatus::Rejected);
    }

    #[test]
    fn exec_on_unknown_order_errors() {
        let mut store = OrderStore::new(4);
        let err = store
            .apply_exec(&ExecEvent {
                cl_ord_id: ClOrdId::from_seq(99),
                exec_id: ExecId::from_bytes([1; 20]),
                reported_status: OrderStatus::Filled,
                last_qty_e2: 100,
                last_px_e9: 1,
            })
            .unwrap_err();
        assert_eq!(err, OrderError::UnknownOrder);
    }

    #[test]
    fn exec_on_terminal_order_errors() {
        let mut store = OrderStore::new(4);
        let id = ClOrdId::from_seq(1);
        store.place(sample_order(id));
        store
            .apply_exec(&ExecEvent {
                cl_ord_id: id,
                exec_id: ExecId::from_bytes([1; 20]),
                reported_status: OrderStatus::Filled,
                last_qty_e2: 10_000,
                last_px_e9: 1,
            })
            .unwrap();

        let err = store
            .apply_exec(&ExecEvent {
                cl_ord_id: id,
                exec_id: ExecId::from_bytes([2; 20]),
                reported_status: OrderStatus::Filled,
                last_qty_e2: 1,
                last_px_e9: 1,
            })
            .unwrap_err();
        assert_eq!(err, OrderError::AlreadyTerminal);
    }

    #[test]
    fn replaying_exec_id_is_idempotent() {
        let mut store = OrderStore::new(4);
        let id = ClOrdId::from_seq(1);
        store.place(sample_order(id));
        let event = ExecEvent {
            cl_ord_id: id,
            exec_id: ExecId::from_bytes([1; 20]),
            reported_status: OrderStatus::Filled,
            last_qty_e2: 10_000,
            last_px_e9: 150_000_000_000,
        };
        let first = store.apply_exec(&event).unwrap();
        assert!(first.is_some());
        let second = store.apply_exec(&event).unwrap();
        assert_eq!(second, None); // deduped, not re-applied
        assert_eq!(store.get(id).unwrap().cum_qty_e2, 10_000); // not double-counted
    }

    proptest! {
        #[test]
        fn fill_sequence_conserves_invariants(fills in prop::collection::vec(1i64..=1_000, 1..10)) {
            let order_qty: i64 = fills.iter().sum();
            let mut store = OrderStore::new(4);
            let id = ClOrdId::from_seq(1);
            let mut order = sample_order(id);
            order.order_qty_e2 = order_qty;
            store.place(order);

            let mut cum = 0i64;
            let mut first_qty = None;
            for (i, qty) in fills.iter().enumerate() {
                cum += *qty;
                first_qty.get_or_insert(*qty);
                let status = if cum == order_qty {
                    OrderStatus::Filled
                } else {
                    OrderStatus::PartiallyFilled
                };
                let fill = store
                    .apply_exec(&ExecEvent {
                        cl_ord_id: id,
                        exec_id: ExecId::from_bytes([i as u8 + 1; 20]),
                        reported_status: status,
                        last_qty_e2: *qty,
                        last_px_e9: 100_000_000_000,
                    })
                    .unwrap();
                prop_assert_eq!(fill.map(|f| f.qty_e2), Some(*qty));
            }

            let final_order = store.get(id).unwrap();
            prop_assert_eq!(final_order.cum_qty_e2, order_qty);
            prop_assert_eq!(final_order.cum_qty_e2 + final_order.leaves_qty_e2, order_qty);
            prop_assert_eq!(final_order.status, OrderStatus::Filled);

            // Replaying any exec_id already applied is a no-op.
            let replay = store
                .apply_exec(&ExecEvent {
                    cl_ord_id: id,
                    exec_id: ExecId::from_bytes([1; 20]),
                    reported_status: OrderStatus::Filled,
                    last_qty_e2: first_qty.unwrap_or(0),
                    last_px_e9: 100_000_000_000,
                })
                .unwrap();
            prop_assert_eq!(replay, None);
            prop_assert_eq!(store.get(id).unwrap().cum_qty_e2, order_qty);
        }
    }
}
