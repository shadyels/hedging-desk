//! Edge converters between `d1-core` types and FIX 4.4 messages. Pure
//! functions only — no session/socket state (that lives in `crate::lib`).
//! ADR-004: proto/bus formats never enter `d1-core`; this module is exactly
//! the boundary where fixed-point `d1-core` types become FIX wire text and
//! back, mirroring `order.rs`'s own doc comment ("what the FIX gateway
//! converts an ExecutionReport into").

use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};

use d1_core::{ClOrdId, ExecEvent, ExecId, Order, OrderStatus, Side};
use quickfix::{FieldMap, Message};

use crate::error::FixError;

const TAG_MSG_TYPE: i32 = 35;
const TAG_CL_ORD_ID: i32 = 11;
const TAG_HANDL_INST: i32 = 21;
const TAG_SYMBOL: i32 = 55;
const TAG_SIDE: i32 = 54;
const TAG_ORD_TYPE: i32 = 40;
const TAG_ORDER_QTY: i32 = 38;
const TAG_PRICE: i32 = 44;
const TAG_EXEC_ID: i32 = 17;
const TAG_ORD_STATUS: i32 = 39;
const TAG_LAST_QTY: i32 = 32;
const TAG_LAST_PX: i32 = 31;

/// Build a `NewOrderSingle` (35=D) from a `d1-core::Order`. Session/header
/// fields (`BeginString`, `SenderCompID`, `TargetCompID`, `MsgSeqNum`,
/// `SendingTime`) are filled by the engine on send
/// (`quickfix::send_to_target`); only `MsgType` and the body are ours to set.
///
/// ponytail: no `TransactTime` (60) — it's a required field per the FIX 4.4
/// spec, but this gateway runs with `UseDataDictionary=N` (no spec-driven
/// validation; see `initiator.cfg`), and formatting a spec `UTCTimestamp`
/// without pulling in a date/time crate (none approved in
/// delta-one/CLAUDE.md's dependency table) buys nothing for a sim-only
/// counterparty. Add a time dependency + this field together if a real venue
/// ever requires strict validation.
pub fn order_to_new_order_single(order: &Order) -> Result<Message, FixError> {
    let mut msg = Message::new();
    msg.with_header_mut(|h| h.set_field(TAG_MSG_TYPE, "D"))?;

    let cl_ord_id = clordid_to_fix(&order.cl_ord_id)?;
    msg.set_field(TAG_CL_ORD_ID, cl_ord_id)?;
    msg.set_field(TAG_HANDL_INST, "1")?; // automated execution, no intervention
    msg.set_field(TAG_SYMBOL, order.instrument.0.to_string())?;
    msg.set_field(TAG_SIDE, side_to_fix(order.side))?;

    // ponytail: `limit_px_e9 == 0` is an in-band "market order" sentinel —
    // it conflates a genuine zero-price limit order. Fine while D1 only emits
    // market orders (Slice 2); add an explicit `OrdType`/enum field on `Order`
    // when real limit orders arrive (P1.M3).
    let is_market = order.limit_px_e9 == 0;
    msg.set_field(TAG_ORD_TYPE, if is_market { "1" } else { "2" })?;
    msg.set_field(TAG_ORDER_QTY, fmt_fixed(order.order_qty_e2, 2))?;
    if !is_market {
        msg.set_field(TAG_PRICE, fmt_fixed(order.limit_px_e9, 9))?;
    }

    Ok(msg)
}

/// Convert an inbound `ExecutionReport` (35=8) to a `d1-core::ExecEvent`.
/// Pure — the gateway is responsible for pushing the result onto the
/// inbound-exec ring.
///
/// `LastQty` (32) and `LastPx` (31) are **conditional** per FIX 4.4 — present
/// only on trade reports (`ExecType` a fill). A status-only report
/// (Rejected / Canceled / New-ack) omits them, so both default to `0` when
/// absent (parsed strictly when present). `apply_exec` (`order.rs`) returns
/// `None` (no fill) when `last_qty_e2 == 0`, so a defaulted-zero status report
/// correctly produces no fill while still transitioning the order's status.
pub fn exec_report_to_event(msg: &Message) -> Result<ExecEvent, FixError> {
    let cl_ord_id_str = msg
        .get_field(TAG_CL_ORD_ID)
        .ok_or(FixError::MissingField(TAG_CL_ORD_ID))?;
    let cl_ord_id = clordid_from_fix(&cl_ord_id_str)?;

    let exec_id_str = msg
        .get_field(TAG_EXEC_ID)
        .ok_or(FixError::MissingField(TAG_EXEC_ID))?;
    let exec_id = exec_id_from_fix(&exec_id_str)?;

    let ord_status_str = msg
        .get_field(TAG_ORD_STATUS)
        .ok_or(FixError::MissingField(TAG_ORD_STATUS))?;
    let reported_status = map_ord_status(&ord_status_str)?;

    let last_qty_e2 = match msg.get_field(TAG_LAST_QTY) {
        Some(s) => parse_fixed(&s, 2, TAG_LAST_QTY)?,
        None => 0,
    };
    let last_px_e9 = match msg.get_field(TAG_LAST_PX) {
        Some(s) => parse_fixed(&s, 9, TAG_LAST_PX)?,
        None => 0,
    };

    Ok(ExecEvent {
        cl_ord_id,
        exec_id,
        reported_status,
        last_qty_e2,
        last_px_e9,
    })
}

fn side_to_fix(side: Side) -> &'static str {
    match side {
        Side::Buy => "1",
        Side::Sell => "2",
    }
}

fn map_ord_status(s: &str) -> Result<OrderStatus, FixError> {
    match s {
        "0" => Ok(OrderStatus::New),
        "1" => Ok(OrderStatus::PartiallyFilled),
        "2" => Ok(OrderStatus::Filled),
        "4" => Ok(OrderStatus::Canceled),
        "6" => Ok(OrderStatus::PendingCancel),
        "8" => Ok(OrderStatus::Rejected),
        "E" => Ok(OrderStatus::PendingReplace),
        other => Err(FixError::UnknownStatus(other.to_string())),
    }
}

/// `d1-core::ClOrdId` is always exactly 20 bytes (`ClOrdId::from_seq`
/// zero-pads), so this never truncates in practice for IDs *we* minted --
/// this handles the general case defensively (an oversize inbound `ClOrdID`
/// is rejected, never truncated, same rationale as `exec_id_from_fix`).
fn clordid_to_fix(id: &ClOrdId) -> Result<&str, FixError> {
    std::str::from_utf8(&id.0).map_err(|_| FixError::Parse {
        tag: TAG_CL_ORD_ID,
        reason: "ClOrdId bytes are not valid UTF-8".to_string(),
    })
}

fn clordid_from_fix(s: &str) -> Result<ClOrdId, FixError> {
    let bytes = s.as_bytes();
    if bytes.len() > 20 {
        return Err(FixError::ClOrdIdTooLong(bytes.len()));
    }
    let mut buf = [b'0'; 20];
    let Some(dest) = buf.get_mut(20 - bytes.len()..) else {
        return Err(FixError::ClOrdIdTooLong(bytes.len()));
    };
    dest.copy_from_slice(bytes);
    Ok(ClOrdId::from_bytes(buf))
}

/// Convert a FIX `ExecID` (tag 17) to our fixed-size `ExecId`.
///
/// FIX `ExecID` is a variable-length string with no 20-byte cap (unlike
/// `ClOrdID`, which this system always mints at exactly 20 bytes itself --
/// see `clordid_from_fix` above). Silently truncating an oversize `ExecID`
/// would risk two distinct execs colliding to the same 20-byte dedupe key
/// (`d1-core::OrderStore::apply_exec` dedupes purely on `ExecId` equality,
/// `ids.rs:48`) -- a truncated collision would look like a replay and
/// silently drop a real fill.
///
/// For `s.len() <= 20` this is unambiguous: right-align into the 20-byte
/// array, same as `clordid_from_fix`. Above 20 bytes -- **hash** rather than
/// reject: a fixed-size digest (via `std`'s `DefaultHasher`, no new
/// dependency) gives an oversize ExecID a (near-certainly) unique dedupe key
/// instead of dropping the fill outright. A dropped fill leaves an order
/// stuck mid-execution forever, which is worse for a trading desk than an
/// astronomically unlikely hash collision; `reject` was the tonight-only
/// placeholder, this is the considered choice.
pub fn exec_id_from_fix(s: &str) -> Result<ExecId, FixError> {
    let bytes = s.as_bytes();
    if bytes.len() <= 20 {
        let mut buf = [b'0'; 20];
        let Some(dest) = buf.get_mut(20 - bytes.len()..) else {
            return Err(FixError::ExecIdTooLong(bytes.len()));
        };
        dest.copy_from_slice(bytes);
        return Ok(ExecId::from_bytes(buf));
    }
    Ok(ExecId::from_bytes(hash20(s)))
}

/// Digest an arbitrary-length string into 20 bytes: three independently
/// salted `DefaultHasher` passes (deterministic -- fixed seed, not
/// `RandomState` -- so the same input always maps to the same `ExecId`
/// within and across process runs), each contributing 8 bytes, truncated to
/// fill the array. ~160 bits of digest, not just a single 64-bit hash, so
/// two ExecIDs that happen to share a 64-bit hash don't collide either.
fn hash20(s: &str) -> [u8; 20] {
    let mut buf = [0u8; 20];
    let mut offset = 0usize;
    for salt in 0u64..3 {
        let mut hasher = DefaultHasher::new();
        salt.hash(&mut hasher);
        s.hash(&mut hasher);
        let digest = hasher.finish().to_le_bytes();
        let n = (20 - offset).min(digest.len());
        if let (Some(dest), Some(src)) = (buf.get_mut(offset..offset + n), digest.get(..n)) {
            dest.copy_from_slice(src);
        }
        offset += n;
    }
    buf
}

/// Format a fixed-point value (`value_e_n`, scaled by `10^decimals`) as a
/// plain decimal string for a FIX field. No floats (root CLAUDE.md's
/// money/quantities invariant) -- quantities/prices are never pricing math,
/// so integer division/modulo does the conversion exactly. Assumes
/// non-negative input (this system never sends negative quantities/prices;
/// direction is `Side`).
///
/// ponytail: the non-negative precondition is currently unguarded — negative
/// input yields malformed FIX text (e.g. `fmt_fixed(-50, 2)` -> `"0.-50"`).
/// Upgrade path is a one-line `debug_assert!(value_e_n >= 0, ...)` (matching
/// the `apply_exec` domain-assert style) if a caller that could ever pass a
/// negative appears. Note only for now — no caller can today.
fn fmt_fixed(value_e_n: i64, decimals: u32) -> String {
    let scale = 10i64.pow(decimals);
    let whole = value_e_n / scale;
    let frac = value_e_n % scale;
    format!("{whole}.{frac:0width$}", width = decimals as usize)
}

/// Inverse of `fmt_fixed`. Tolerates fewer or more fractional digits than
/// `decimals` (pads/truncates) rather than rejecting mismatched precision --
/// ponytail: sim never sends more precision than we format, so this
/// leniency costs nothing today; tighten if a real venue's precision needs
/// to be preserved exactly.
fn parse_fixed(s: &str, decimals: u32, tag: i32) -> Result<i64, FixError> {
    let scale = 10i64.pow(decimals);
    let (whole_str, frac_str) = s.split_once('.').unwrap_or((s, ""));
    let whole: i64 = whole_str.parse().map_err(|_| FixError::Parse {
        tag,
        reason: format!("bad integer part {whole_str:?}"),
    })?;

    let mut frac_str = frac_str.to_string();
    frac_str.truncate(decimals as usize);
    while frac_str.len() < decimals as usize {
        frac_str.push('0');
    }
    let frac: i64 = if frac_str.is_empty() {
        0
    } else {
        frac_str.parse().map_err(|_| FixError::Parse {
            tag,
            reason: format!("bad fractional part {frac_str:?}"),
        })?
    };

    whole
        .checked_mul(scale)
        .and_then(|w| w.checked_add(frac))
        .ok_or_else(|| FixError::Parse {
            tag,
            reason: "overflow converting to fixed-point".to_string(),
        })
}

#[cfg(test)]
#[allow(clippy::unwrap_used)] // tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::*;
    use d1_core::{BookId, InstrumentId, OrderStatus};

    fn sample_order() -> Order {
        Order {
            cl_ord_id: ClOrdId::from_seq(42),
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
    fn order_to_new_order_single_sets_required_fields() {
        let msg = order_to_new_order_single(&sample_order()).unwrap();
        assert_eq!(
            msg.with_header(|h| h.get_field(TAG_MSG_TYPE)),
            Some("D".to_string())
        );
        assert_eq!(
            msg.get_field(TAG_CL_ORD_ID),
            Some("00000000000000000042".to_string())
        );
        assert_eq!(msg.get_field(TAG_SYMBOL), Some("1001".to_string()));
        assert_eq!(msg.get_field(TAG_SIDE), Some("1".to_string()));
        assert_eq!(msg.get_field(TAG_ORD_TYPE), Some("1".to_string())); // market: limit_px_e9 == 0
        assert_eq!(msg.get_field(TAG_ORDER_QTY), Some("100.00".to_string()));
        assert_eq!(msg.get_field(TAG_PRICE), None); // market order: no Price
    }

    #[test]
    fn order_to_new_order_single_limit_sets_price() {
        let mut order = sample_order();
        order.limit_px_e9 = 150_500_000_000;
        let msg = order_to_new_order_single(&order).unwrap();
        assert_eq!(msg.get_field(TAG_ORD_TYPE), Some("2".to_string()));
        assert_eq!(msg.get_field(TAG_PRICE), Some("150.500000000".to_string()));
    }

    #[test]
    fn map_ord_status_covers_all_variants() {
        assert_eq!(map_ord_status("0").unwrap(), OrderStatus::New);
        assert_eq!(map_ord_status("1").unwrap(), OrderStatus::PartiallyFilled);
        assert_eq!(map_ord_status("2").unwrap(), OrderStatus::Filled);
        assert_eq!(map_ord_status("4").unwrap(), OrderStatus::Canceled);
        assert_eq!(map_ord_status("6").unwrap(), OrderStatus::PendingCancel);
        assert_eq!(map_ord_status("8").unwrap(), OrderStatus::Rejected);
        assert_eq!(map_ord_status("E").unwrap(), OrderStatus::PendingReplace);
        assert!(matches!(
            map_ord_status("Z"),
            Err(FixError::UnknownStatus(s)) if s == "Z"
        ));
    }

    fn sample_exec_report() -> Message {
        let mut msg = Message::new();
        msg.with_header_mut(|h| h.set_field(TAG_MSG_TYPE, "8"))
            .unwrap();
        msg.set_field(TAG_CL_ORD_ID, "00000000000000000042")
            .unwrap();
        msg.set_field(TAG_EXEC_ID, "EXEC-1").unwrap();
        msg.set_field(TAG_ORD_STATUS, "2").unwrap();
        msg.set_field(TAG_LAST_QTY, "100.00").unwrap();
        msg.set_field(TAG_LAST_PX, "150.500000000").unwrap();
        msg
    }

    #[test]
    fn exec_report_to_event_round_trips() {
        let event = exec_report_to_event(&sample_exec_report()).unwrap();
        assert_eq!(event.cl_ord_id, ClOrdId::from_seq(42));
        assert_eq!(event.reported_status, OrderStatus::Filled);
        assert_eq!(event.last_qty_e2, 10_000);
        assert_eq!(event.last_px_e9, 150_500_000_000);
    }

    #[test]
    fn exec_report_to_event_status_only_defaults_qty_and_px_to_zero() {
        // A Rejected (39=8) report with no LastQty (32) / LastPx (31) --
        // conditional fields, absent on a status-only report. Must convert
        // cleanly (not error) and default the money fields to 0.
        let mut msg = Message::new();
        msg.with_header_mut(|h| h.set_field(TAG_MSG_TYPE, "8"))
            .unwrap();
        msg.set_field(TAG_CL_ORD_ID, "00000000000000000042")
            .unwrap();
        msg.set_field(TAG_EXEC_ID, "EXEC-1").unwrap();
        msg.set_field(TAG_ORD_STATUS, "8").unwrap();

        let event = exec_report_to_event(&msg).unwrap();
        assert_eq!(event.reported_status, OrderStatus::Rejected);
        assert_eq!(event.last_qty_e2, 0);
        assert_eq!(event.last_px_e9, 0);
    }

    #[test]
    fn exec_report_to_event_missing_field_errors() {
        let mut msg = sample_exec_report();
        msg.remove_field(TAG_ORD_STATUS).unwrap();
        assert!(matches!(
            exec_report_to_event(&msg),
            Err(FixError::MissingField(TAG_ORD_STATUS))
        ));
    }

    #[test]
    fn exec_id_from_fix_short_id_right_aligns_zero_padded() {
        let id = exec_id_from_fix("EXEC-1").unwrap();
        // "EXEC-1" is 6 bytes; right-aligned into 20 means 14 leading zeros.
        assert_eq!(id, ExecId::from_bytes(*b"00000000000000EXEC-1"));
    }

    #[test]
    fn exec_id_from_fix_oversize_hashes_deterministically_without_naive_truncation() {
        // `a` and `b` share the same first 20 bytes and only differ after
        // that -- a naive truncate-to-20 would collide them, which is
        // exactly the silently-dropped-fill bug this function exists to
        // avoid (see its doc comment).
        let a = "E".repeat(21);
        let b = format!("{}X", "E".repeat(21));

        let id_a1 = exec_id_from_fix(&a).unwrap();
        let id_a2 = exec_id_from_fix(&a).unwrap();
        let id_b = exec_id_from_fix(&b).unwrap();

        assert_eq!(id_a1, id_a2, "hashing must be deterministic");
        assert_ne!(
            id_a1, id_b,
            "distinct oversize ids sharing a 20-byte prefix must not collide"
        );
    }

    #[test]
    fn clordid_from_fix_rejects_oversize() {
        let oversize = "1".repeat(21);
        assert!(matches!(
            clordid_from_fix(&oversize),
            Err(FixError::ClOrdIdTooLong(21))
        ));
    }

    #[test]
    fn fmt_fixed_and_parse_fixed_round_trip() {
        let s = fmt_fixed(150_500_000_000, 9);
        assert_eq!(s, "150.500000000");
        assert_eq!(parse_fixed(&s, 9, TAG_LAST_PX).unwrap(), 150_500_000_000);
    }
}
