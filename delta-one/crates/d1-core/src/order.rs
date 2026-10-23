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
    // ponytail: not driven — FIX 35=F (OrderCancelRequest) lands with the
    // P3.M2 kill switch, its first real consumer.
    PendingCancel,
    /// A cancel/replace request is in flight.
    // ponytail: not driven — FIX 35=G (OrderCancelReplaceRequest): no
    // roadmap consumer (kill switch only cancels); owner undecided.
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

/// Core -> NATS DTO: everything `d1-gateway-nats::convert::exec_report_to_pb`
/// needs to build a proto `ExecutionReport`. Built by the core thread from
/// the post-`apply_exec` `Order` plus the `ExecEvent` that produced it. Plain
/// struct -- ADR-004, proto stays out of `d1-core`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ExecReport {
    /// Which order this report describes.
    pub cl_ord_id: ClOrdId,
    /// The specific exec (fill or status-only change) this report carries.
    pub exec_id: ExecId,
    /// Book the order belongs to.
    pub book: BookId,
    /// Instrument traded.
    pub instrument: InstrumentId,
    /// Buy or sell.
    pub side: Side,
    /// Order status after this exec.
    pub status: OrderStatus,
    /// Quantity filled by this specific exec (0 for a non-fill status
    /// change), fixed-point x10^2.
    pub last_qty_e2: i64,
    /// Price of this specific fill, fixed-point x10^9.
    pub last_px_e9: i64,
    /// Cumulative filled quantity, fixed-point x10^2.
    pub cum_qty_e2: i64,
    /// Remaining quantity, fixed-point x10^2.
    pub leaves_qty_e2: i64,
}

/// Outcome of `apply_exec`: whether the exec was a replayed duplicate
/// (idempotent no-op, root CLAUDE.md invariant #4) or was actually applied
/// to the order. `Ok(None)` used to conflate these two cases -- a replayed
/// `ExecId` and a genuine non-fill status transition both returned it,
/// which let a duplicate exec masquerade as a real state transition at
/// every caller (e.g. `crates/d1/src/lib.rs::run_core` emitting a fresh,
/// undedupable `posttrade.orders.audit` record for a FIX PossDup resend).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExecOutcome {
    /// `ExecId` already seen -- no state changed, caller must emit nothing.
    Duplicate,
    /// Applied; `Some(Fill)` if the exec carried fill quantity, `None` for
    /// a genuine non-fill status transition (e.g. a reject).
    Applied(Option<Fill>),
    /// Applied, but `last_qty_e2` exceeded the order's leaves. Only `leaves`
    /// was booked (`fill`; `None` if leaves was already 0), the order is
    /// terminal, and `excess_qty_e2` (> 0) is booked NOWHERE -- the caller
    /// must surface it.
    Overfilled {
        /// The booked part (`min(last_qty, leaves)`), if non-zero.
        fill: Option<Fill>,
        /// Quantity beyond leaves that no book holds.
        excess_qty_e2: i64,
    },
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
    // ponytail: append-only slab, no slot reuse for terminal orders, and
    // `seen_execs` likewise grows without bound — fine while capacity is
    // sized generously for a single session; a freelist reclaiming
    // terminal-order slots (and an eviction policy for `seen_execs`) lands
    // if/when long-uptime memory bounding actually matters (Slice 2/3, once
    // a real gateway drives this continuously).
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
    /// no-op, returns `ExecOutcome::Duplicate`), reject execs on an unknown
    /// or already-terminal order, validate the transition, update
    /// `cum`/`leaves`/`status`, and return `ExecOutcome::Applied(Some(Fill))`
    /// when the exec carried a fill quantity or `ExecOutcome::Applied(None)`
    /// for a genuine non-fill status transition. A fill quantity above the
    /// order's leaves books only `leaves`, ends the order terminal and
    /// returns `ExecOutcome::Overfilled` carrying the unbooked excess; a
    /// negative fill quantity is `OrderError::NegativeFillQty`.
    pub fn apply_exec(&mut self, event: &ExecEvent) -> Result<ExecOutcome, OrderError> {
        if self.seen_execs.contains(&event.exec_id) {
            return Ok(ExecOutcome::Duplicate);
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
            (
                OrderStatus::New | OrderStatus::PartiallyFilled,
                OrderStatus::PartiallyFilled
                | OrderStatus::Filled
                | OrderStatus::Rejected
                | OrderStatus::Canceled,
            ) => {}
            (from, to) => return Err(OrderError::IllegalTransition { from, to }),
        }

        // Negative fill (root CLAUDE.md #7): error before any mutation and
        // before `seen_execs.insert`, so a rejected exec is not marked seen.
        // `ExecEvent` has pub fields, so it can arrive from any adapter.
        if event.last_qty_e2 < 0 {
            return Err(OrderError::NegativeFillQty);
        }
        // Overfill (last_qty > leaves): book only `leaves`, end the order
        // terminal (`Filled` unless the venue reported Rejected/Canceled)
        // so the parent's inflight clears, mark the exec seen, and return
        // the unbooked remainder as `ExecOutcome::Overfilled`.
        // ponytail: the overfill excess is surfaced, not booked -- the core
        // loop logs it and flags the order-audit `detail`, but no book holds
        // it, so the firm position differs from the venue's by
        // `excess_qty_e2`. Booking it needs `allocate_fill` (d1/cycle.rs) to
        // place quantity beyond each book's `m_b`. Status/leaves consistency
        // (Filled with leaves > 0, PartiallyFilled that exactly exhausts
        // leaves) stays a `debug_assert!`. Decide both before the first
        // non-sim venue (P5 venue dialects).
        debug_assert!(
            order.leaves_qty_e2 >= 0,
            "leaves_qty_e2 went negative (place() does not validate order_qty)"
        );
        let booked = event.last_qty_e2.min(order.leaves_qty_e2);
        let excess = event
            .last_qty_e2
            .checked_sub(booked)
            .ok_or(OrderError::Overflow)?;
        let new_cum = order
            .cum_qty_e2
            .checked_add(booked)
            .ok_or(OrderError::Overflow)?;
        let new_leaves = order
            .leaves_qty_e2
            .checked_sub(booked)
            .ok_or(OrderError::Overflow)?;
        let status = if excess > 0 && !event.reported_status.is_terminal() {
            OrderStatus::Filled
        } else {
            event.reported_status
        };
        debug_assert!(
            status != OrderStatus::Filled || new_leaves == 0,
            "reported Filled but leaves_qty is nonzero"
        );
        debug_assert!(
            status != OrderStatus::PartiallyFilled || new_leaves > 0,
            "reported PartiallyFilled but leaves_qty is zero"
        );

        order.cum_qty_e2 = new_cum;
        order.leaves_qty_e2 = new_leaves;
        order.status = status;
        let fill = if booked > 0 {
            order.last_px_e9 = event.last_px_e9;
            Some(Fill {
                book: order.book,
                instrument: order.instrument,
                side: order.side,
                qty_e2: booked,
                px_e9: event.last_px_e9,
            })
        } else {
            None
        };

        self.seen_execs.insert(event.exec_id);
        if excess > 0 {
            return Ok(ExecOutcome::Overfilled {
                fill,
                excess_qty_e2: excess,
            });
        }
        Ok(ExecOutcome::Applied(fill))
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
            ExecOutcome::Applied(Some(Fill {
                book: BookId(1),
                instrument: InstrumentId(1001),
                side: Side::Buy,
                qty_e2: 4_000,
                px_e9: 150_000_000_000,
            }))
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
        assert_eq!(
            fill2,
            ExecOutcome::Applied(Some(Fill {
                book: BookId(1),
                instrument: InstrumentId(1001),
                side: Side::Buy,
                qty_e2: 6_000,
                px_e9: 150_500_000_000,
            }))
        );
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
        assert_eq!(fill, ExecOutcome::Applied(None));
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

    fn exec(id: ClOrdId, exec_id: u8, status: OrderStatus, qty: i64) -> ExecEvent {
        ExecEvent {
            cl_ord_id: id,
            exec_id: ExecId::from_bytes([exec_id; 20]),
            reported_status: status,
            last_qty_e2: qty,
            last_px_e9: 1,
        }
    }

    #[test]
    fn overfill_books_leaves_and_reports_excess() {
        let mut store = OrderStore::new(4);
        let id = ClOrdId::from_seq(1);
        store.place(sample_order(id)); // order_qty_e2 = 10_000
        let out = store
            .apply_exec(&exec(id, 1, OrderStatus::Filled, 12_000))
            .unwrap();
        assert_eq!(
            out,
            ExecOutcome::Overfilled {
                fill: Some(Fill {
                    book: BookId(1),
                    instrument: InstrumentId(1001),
                    side: Side::Buy,
                    qty_e2: 10_000,
                    px_e9: 1,
                }),
                excess_qty_e2: 2_000,
            }
        );
        let o = store.get(id).unwrap();
        assert_eq!(o.status, OrderStatus::Filled);
        assert_eq!(
            (o.cum_qty_e2, o.leaves_qty_e2, o.last_px_e9),
            (10_000, 0, 1)
        );
        // The overfill exec was applied, so a replay is a Duplicate.
        let replay = store
            .apply_exec(&exec(id, 1, OrderStatus::Filled, 12_000))
            .unwrap();
        assert_eq!(replay, ExecOutcome::Duplicate);
        assert_eq!(store.get(id).unwrap().cum_qty_e2, 10_000);
    }

    #[test]
    fn overfill_reported_partially_filled_ends_filled() {
        let mut store = OrderStore::new(4);
        let id = ClOrdId::from_seq(1);
        store.place(sample_order(id));
        store
            .apply_exec(&exec(id, 1, OrderStatus::PartiallyFilled, 4_000))
            .unwrap();
        let out = store
            .apply_exec(&exec(id, 2, OrderStatus::PartiallyFilled, 7_000))
            .unwrap();
        assert!(
            matches!(
                out,
                ExecOutcome::Overfilled {
                    fill: Some(Fill { qty_e2: 6_000, .. }),
                    excess_qty_e2: 1_000
                }
            ),
            "got {out:?}"
        );
        assert_eq!(store.get(id).unwrap().status, OrderStatus::Filled);
        let err = store
            .apply_exec(&exec(id, 3, OrderStatus::Filled, 1))
            .unwrap_err();
        assert_eq!(err, OrderError::AlreadyTerminal);
    }

    #[test]
    fn negative_fill_qty_is_rejected_without_mutation() {
        let mut store = OrderStore::new(4);
        let id = ClOrdId::from_seq(1);
        store.place(sample_order(id));
        let before = store.get(id).unwrap();
        let err = store
            .apply_exec(&exec(id, 1, OrderStatus::PartiallyFilled, -1))
            .unwrap_err();
        assert_eq!(err, OrderError::NegativeFillQty);
        assert_eq!(store.get(id).unwrap(), before);
        let ok = store
            .apply_exec(&exec(id, 1, OrderStatus::PartiallyFilled, 5_000))
            .unwrap();
        assert!(matches!(ok, ExecOutcome::Applied(Some(_))));
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
        assert!(matches!(first, ExecOutcome::Applied(Some(_))));
        // A redelivered ExecId (FIX PossDup / venue re-send, root CLAUDE.md
        // #4) must come back as `Duplicate`, never re-`Applied` -- the
        // caller distinguishes this from a genuine non-fill transition to
        // decide whether to emit an audit record (crates/d1/src/lib.rs).
        let second = store.apply_exec(&event).unwrap();
        assert_eq!(second, ExecOutcome::Duplicate); // deduped, not re-applied
        assert_eq!(store.get(id).unwrap().cum_qty_e2, 10_000); // not double-counted
    }

    proptest! {
        #[test]
        fn negative_fill_never_mutates_order(
            order_qty in 1i64..=1_000_000,
            partial_seed in 0i64..1_000_000,
            neg in -1_000_000i64..=-1,
        ) {
            let mut store = OrderStore::new(4);
            let id = ClOrdId::from_seq(1);
            let mut order = sample_order(id);
            order.order_qty_e2 = order_qty;
            store.place(order);
            let p = partial_seed % order_qty; // 0..order_qty
            if p > 0 {
                store.apply_exec(&exec(id, 1, OrderStatus::PartiallyFilled, p)).unwrap();
            }
            let leaves = order_qty - p;
            let before = store.get(id).unwrap();
            let res = store.apply_exec(&exec(id, 2, OrderStatus::PartiallyFilled, neg));
            prop_assert_eq!(res, Err(OrderError::NegativeFillQty));
            prop_assert_eq!(store.get(id).unwrap(), before);
            // Not marked seen: the same exec id with a valid qty applies.
            let ok = store.apply_exec(&exec(id, 2, OrderStatus::Filled, leaves)).unwrap();
            prop_assert!(matches!(ok, ExecOutcome::Applied(Some(_))));
        }

        #[test]
        fn overfill_books_exactly_leaves(
            order_qty in 1i64..=1_000_000,
            partial_seed in 0i64..1_000_000,
            over_extra in 1i64..=1_000_000,
            reported in prop_oneof![
                Just(OrderStatus::PartiallyFilled),
                Just(OrderStatus::Filled),
                Just(OrderStatus::Canceled),
            ],
        ) {
            let mut store = OrderStore::new(4);
            let id = ClOrdId::from_seq(1);
            let mut order = sample_order(id);
            order.order_qty_e2 = order_qty;
            store.place(order);
            let p = partial_seed % order_qty;
            if p > 0 {
                store.apply_exec(&exec(id, 1, OrderStatus::PartiallyFilled, p)).unwrap();
            }
            let leaves = order_qty - p;
            let ev = exec(id, 2, reported, leaves + over_extra);
            match store.apply_exec(&ev).unwrap() {
                ExecOutcome::Overfilled { fill: Some(f), excess_qty_e2 } => {
                    prop_assert_eq!(f.qty_e2, leaves);
                    prop_assert_eq!(excess_qty_e2, over_extra);
                }
                other => prop_assert!(false, "expected Overfilled, got {:?}", other),
            }
            let o = store.get(id).unwrap();
            let expected = if reported.is_terminal() { reported } else { OrderStatus::Filled };
            prop_assert_eq!((o.cum_qty_e2, o.leaves_qty_e2, o.status), (order_qty, 0, expected));
            prop_assert_eq!(store.apply_exec(&ev).unwrap(), ExecOutcome::Duplicate);
            prop_assert_eq!(store.get(id).unwrap().cum_qty_e2, order_qty);
        }

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
                let outcome = store
                    .apply_exec(&ExecEvent {
                        cl_ord_id: id,
                        exec_id: ExecId::from_bytes([i as u8 + 1; 20]),
                        reported_status: status,
                        last_qty_e2: *qty,
                        last_px_e9: 100_000_000_000,
                    })
                    .unwrap();
                let fill = match outcome {
                    ExecOutcome::Applied(f) => f,
                    other => {
                        prop_assert!(false, "expected Applied, got {:?}", other);
                        None
                    }
                };
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
            prop_assert_eq!(replay, ExecOutcome::Duplicate);
            prop_assert_eq!(store.get(id).unwrap().cum_qty_e2, order_qty);
        }
    }
}
