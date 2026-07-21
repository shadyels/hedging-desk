//! Edge converter between `d1-posttrade`'s ring payload types and the Avro
//! wire contract (`protocol/avro/`). Pure mapping only — no producer/thread
//! state (Slice 2 owns that); mirrors `d1-gateway-nats::convert`'s role for
//! the NATS/Protobuf side.

use std::time::{SystemTime, UNIX_EPOCH};

use apache_avro::types::Value;
use apache_avro::{Schema, to_avro_datum};
use d1_core::{ClOrdId, ExecId, OrderStatus, Side};
use d1_refdata::Universe;
use uuid::Uuid;

use crate::{
    Allocation, Cross, NettingCycleId, OrderAudit, PostTradeError, PostTradeEvent, TradeKind,
    TradeLeg,
};

/// Parsed Avro schemas for the four `protocol/avro/` post-trade records,
/// parsed once at construction (`Schema::parse_str` is not free — do it
/// once, not per encode call).
pub struct Schemas {
    trade: Schema,
    cross: Schema,
    allocation: Schema,
    order_audit: Schema,
}

impl Schemas {
    /// Parse all four post-trade Avro schemas from `protocol/avro/`.
    pub fn new() -> Result<Self, PostTradeError> {
        Ok(Self {
            trade: Schema::parse_str(include_str!(
                "../../../../protocol/avro/posttrade_trade.avsc"
            ))?,
            cross: Schema::parse_str(include_str!(
                "../../../../protocol/avro/posttrade_cross.avsc"
            ))?,
            allocation: Schema::parse_str(include_str!(
                "../../../../protocol/avro/posttrade_allocation.avsc"
            ))?,
            order_audit: Schema::parse_str(include_str!(
                "../../../../protocol/avro/order_audit.avsc"
            ))?,
        })
    }

    /// Encode one post-trade event as an Avro binary datum against its
    /// record schema. Resolves `symbol`/`currency` from `uni`; errors if the
    /// event names an instrument not in the universe. Mints `msg_id` (and
    /// `trade_id`/`allocation_id`) fresh (UUIDv7) and stamps `booked_ns`/
    /// `ts_ns` from the wall clock — this is the edge, off the hot path.
    ///
    /// **Not idempotent — call exactly once per event.** Each call mints a
    /// fresh `msg_id`/`trade_id`/`allocation_id` and timestamp, so encoding
    /// the same event twice produces two different dedupe keys (root
    /// invariant #4) and two different business ids. Contrast `cross_id`,
    /// which is minted once at booking time and passed through unchanged —
    /// its lineage is stable across calls. Slice 2's producer must retry the
    /// bytes this returns, never re-`encode()` the same event.
    pub fn encode(
        &self,
        event: &PostTradeEvent,
        uni: &Universe,
    ) -> Result<Vec<u8>, PostTradeError> {
        match event {
            PostTradeEvent::Trade(t) => encode_trade(&self.trade, t, uni),
            PostTradeEvent::Cross(c) => encode_cross(&self.cross, c, uni),
            PostTradeEvent::Allocation(a) => encode_allocation(&self.allocation, a, uni),
            PostTradeEvent::OrderAudit(o) => encode_order_audit(&self.order_audit, o),
        }
    }
}

fn encode_trade(schema: &Schema, t: &TradeLeg, uni: &Universe) -> Result<Vec<u8>, PostTradeError> {
    let symbol = resolve_symbol(uni, t.instrument)?;
    let currency = resolve_currency(uni, t.instrument)?;

    let value = Value::Record(vec![
        (
            "msg_id".to_string(),
            Value::String(Uuid::now_v7().to_string()),
        ),
        (
            "trade_id".to_string(),
            Value::String(Uuid::now_v7().to_string()),
        ),
        ("booked_ns".to_string(), Value::Long(now_ns()?)),
        ("book_id".to_string(), Value::Int(id_to_i32(t.book.0)?)),
        (
            "instrument_id".to_string(),
            Value::Int(id_to_i32(t.instrument.0)?),
        ),
        ("symbol".to_string(), Value::String(symbol)),
        ("side".to_string(), side_enum(t.side)),
        ("qty_e2".to_string(), Value::Long(t.qty_e2)),
        ("px_e9".to_string(), Value::Long(t.px_e9)),
        ("currency".to_string(), Value::String(currency)),
        ("trade_kind".to_string(), trade_kind_enum(t.kind)),
        (
            "cross_id".to_string(),
            nullable_string(t.cross_id.map(|id| id.to_string())),
        ),
        (
            "parent_cl_ord_id".to_string(),
            nullable_string(t.parent_cl_ord_id.map(|id| clordid_to_string(&id))),
        ),
        (
            "exec_id".to_string(),
            nullable_string(t.exec_id.map(|id| execid_to_string(&id))),
        ),
        (
            "counterparty".to_string(),
            Value::String(t.counterparty.to_string()),
        ),
    ]);

    Ok(to_avro_datum(schema, value)?)
}

fn encode_cross(schema: &Schema, c: &Cross, uni: &Universe) -> Result<Vec<u8>, PostTradeError> {
    let symbol = resolve_symbol(uni, c.instrument)?;

    let value = Value::Record(vec![
        (
            "msg_id".to_string(),
            Value::String(Uuid::now_v7().to_string()),
        ),
        (
            "cross_id".to_string(),
            Value::String(c.cross_id.to_string()),
        ),
        ("booked_ns".to_string(), Value::Long(now_ns()?)),
        (
            "instrument_id".to_string(),
            Value::Int(id_to_i32(c.instrument.0)?),
        ),
        ("symbol".to_string(), Value::String(symbol)),
        (
            "buy_book_id".to_string(),
            Value::Int(id_to_i32(c.buy_book.0)?),
        ),
        (
            "sell_book_id".to_string(),
            Value::Int(id_to_i32(c.sell_book.0)?),
        ),
        ("qty_e2".to_string(), Value::Long(c.qty_e2)),
        ("ref_px_e9".to_string(), Value::Long(c.ref_px_e9)),
        (
            "px_policy_id".to_string(),
            Value::String(c.policy_id.to_string()),
        ),
        (
            "netting_cycle_id".to_string(),
            Value::String(netting_cycle_id_str(c.netting_cycle_id)),
        ),
    ]);

    Ok(to_avro_datum(schema, value)?)
}

fn encode_allocation(
    schema: &Schema,
    a: &Allocation,
    uni: &Universe,
) -> Result<Vec<u8>, PostTradeError> {
    // Referenced only to confirm the instrument resolves in the universe
    // (allocations don't carry `symbol` on the wire, unlike trades/crosses).
    resolve_symbol(uni, a.instrument)?;

    let value = Value::Record(vec![
        (
            "msg_id".to_string(),
            Value::String(Uuid::now_v7().to_string()),
        ),
        (
            "allocation_id".to_string(),
            Value::String(Uuid::now_v7().to_string()),
        ),
        ("booked_ns".to_string(), Value::Long(now_ns()?)),
        (
            "parent_cl_ord_id".to_string(),
            Value::String(clordid_to_string(&a.parent_cl_ord_id)),
        ),
        (
            "exec_id".to_string(),
            Value::String(execid_to_string(&a.exec_id)),
        ),
        (
            "instrument_id".to_string(),
            Value::Int(id_to_i32(a.instrument.0)?),
        ),
        ("book_id".to_string(), Value::Int(id_to_i32(a.book.0)?)),
        ("qty_e2".to_string(), Value::Long(a.qty_e2)),
        ("px_e9".to_string(), Value::Long(a.px_e9)),
        (
            "netting_cycle_id".to_string(),
            Value::String(netting_cycle_id_str(a.netting_cycle_id)),
        ),
    ]);

    Ok(to_avro_datum(schema, value)?)
}

fn encode_order_audit(schema: &Schema, o: &OrderAudit) -> Result<Vec<u8>, PostTradeError> {
    let value = Value::Record(vec![
        (
            "msg_id".to_string(),
            Value::String(Uuid::now_v7().to_string()),
        ),
        ("ts_ns".to_string(), Value::Long(now_ns()?)),
        (
            "cl_ord_id".to_string(),
            Value::String(clordid_to_string(&o.cl_ord_id)),
        ),
        (
            "instrument_id".to_string(),
            Value::Int(id_to_i32(o.instrument.0)?),
        ),
        ("side".to_string(), side_enum(o.side)),
        (
            "transition".to_string(),
            Value::String(format!(
                "{}->{}",
                status_str(o.from_status),
                status_str(o.to_status)
            )),
        ),
        ("qty_e2".to_string(), Value::Long(o.qty_e2)),
        ("cum_qty_e2".to_string(), Value::Long(o.cum_qty_e2)),
        ("leaves_qty_e2".to_string(), Value::Long(o.leaves_qty_e2)),
        ("origin".to_string(), origin_enum(o.origin)),
        (
            "detail".to_string(),
            nullable_string(o.detail.map(std::string::ToString::to_string)),
        ),
    ]);

    Ok(to_avro_datum(schema, value)?)
}

fn resolve_symbol(
    uni: &Universe,
    instrument: d1_core::InstrumentId,
) -> Result<String, PostTradeError> {
    uni.id_to_symbol
        .get(&instrument)
        .cloned()
        .ok_or(PostTradeError::UnknownInstrument(instrument))
}

fn resolve_currency(
    uni: &Universe,
    instrument: d1_core::InstrumentId,
) -> Result<String, PostTradeError> {
    uni.id_to_currency
        .get(&instrument)
        .cloned()
        .ok_or(PostTradeError::UnknownInstrument(instrument))
}

fn id_to_i32(id: u32) -> Result<i32, PostTradeError> {
    i32::try_from(id).map_err(|_| PostTradeError::IdOverflow(id))
}

fn now_ns() -> Result<i64, PostTradeError> {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(PostTradeError::ClockBeforeEpoch)?;
    Ok(i64::try_from(elapsed.as_nanos()).unwrap_or(i64::MAX))
}

fn nullable_string(value: Option<String>) -> Value {
    match value {
        Some(s) => Value::Union(1, Box::new(Value::String(s))),
        None => Value::Union(0, Box::new(Value::Null)),
    }
}

fn side_enum(side: Side) -> Value {
    match side {
        Side::Buy => Value::Enum(0, "BUY".to_string()),
        Side::Sell => Value::Enum(1, "SELL".to_string()),
    }
}

fn trade_kind_enum(kind: TradeKind) -> Value {
    match kind {
        TradeKind::ExternalFill => Value::Enum(0, "EXTERNAL_FILL".to_string()),
        TradeKind::InternalCrossLeg => Value::Enum(1, "INTERNAL_CROSS_LEG".to_string()),
    }
}

fn origin_enum(origin: crate::AuditOrigin) -> Value {
    match origin {
        crate::AuditOrigin::NettingEngine => Value::Enum(0, "NETTING_ENGINE".to_string()),
        crate::AuditOrigin::ManualUi => Value::Enum(1, "MANUAL_UI".to_string()),
        crate::AuditOrigin::System => Value::Enum(2, "SYSTEM".to_string()),
    }
}

fn netting_cycle_id_str(id: NettingCycleId) -> String {
    match id {
        NettingCycleId::Cycle(n) => n.to_string(),
        NettingCycleId::Direct => "DIRECT".to_string(),
    }
}

/// `d1-core` ids are fixed 20-byte ASCII arrays; the Avro fields are plain
/// strings with no length cap, so a lossy decode is exact for every id this
/// system ever mints (mirrors `d1-gateway-nats::convert::clordid_to_string`).
///
/// `pub(crate)`: `lib.rs::topic_and_key` reuses this so the Kafka partition
/// key matches the Avro `parent_cl_ord_id`/`cl_ord_id` field byte-for-byte.
pub(crate) fn clordid_to_string(id: &ClOrdId) -> String {
    String::from_utf8_lossy(&id.0).into_owned()
}

fn execid_to_string(id: &ExecId) -> String {
    String::from_utf8_lossy(&id.0).into_owned()
}

/// `OrderStatus` -> SCREAMING_SNAKE, for the `transition` string. Kept local
/// to this converter (not on `d1-core::OrderStatus`) since it's a wire-format
/// concern, mirroring how `d1-gateway-nats::convert` keeps its
/// proto-enum mapping local too.
fn status_str(status: OrderStatus) -> &'static str {
    match status {
        OrderStatus::New => "NEW",
        OrderStatus::PartiallyFilled => "PARTIALLY_FILLED",
        OrderStatus::Filled => "FILLED",
        OrderStatus::Rejected => "REJECTED",
        OrderStatus::Canceled => "CANCELED",
        OrderStatus::PendingCancel => "PENDING_CANCEL",
        OrderStatus::PendingReplace => "PENDING_REPLACE",
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)] // tests: hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use std::collections::HashMap;

    use apache_avro::from_avro_datum;
    use d1_core::{BookId, InstrumentId};

    use super::*;
    use crate::{AuditOrigin, PostTradeEvent};

    fn test_universe() -> Universe {
        let mut symbol_to_id = HashMap::new();
        let mut id_to_symbol = HashMap::new();
        let mut id_to_currency = HashMap::new();
        symbol_to_id.insert("AAPL".to_string(), InstrumentId(1001));
        id_to_symbol.insert(InstrumentId(1001), "AAPL".to_string());
        id_to_currency.insert(InstrumentId(1001), "USD".to_string());

        Universe {
            book_ids: vec![BookId(1), BookId(2)],
            instrument_ids: vec![InstrumentId(1001)],
            symbol_to_id,
            id_to_symbol,
            id_to_currency,
            cross_px_policy: "ARRIVAL_MID".to_string(),
        }
    }

    fn field<'a>(record: &'a Value, name: &str) -> &'a Value {
        match record {
            Value::Record(fields) => &fields.iter().find(|(n, _)| n == name).unwrap().1,
            _ => panic!("expected a Value::Record"),
        }
    }

    fn assert_valid_uuid(value: &Value) {
        let Value::String(s) = value else {
            panic!("expected a Value::String")
        };
        let parsed = Uuid::parse_str(s).unwrap();
        assert!(!parsed.is_nil());
    }

    #[test]
    fn encode_decode_trade_internal_cross_leg() {
        let schemas = Schemas::new().unwrap();
        let uni = test_universe();
        let cross_id = Uuid::now_v7();
        let leg = TradeLeg {
            book: BookId(1),
            instrument: InstrumentId(1001),
            side: Side::Buy,
            qty_e2: 80_000,
            px_e9: 150_000_000_000,
            kind: TradeKind::InternalCrossLeg,
            cross_id: Some(cross_id),
            parent_cl_ord_id: None,
            exec_id: None,
            counterparty: "INTERNAL",
        };

        let bytes = schemas.encode(&PostTradeEvent::Trade(leg), &uni).unwrap();
        let decoded = from_avro_datum(&schemas.trade, &mut &bytes[..], None).unwrap();

        assert_valid_uuid(field(&decoded, "msg_id"));
        assert_valid_uuid(field(&decoded, "trade_id"));
        assert!(matches!(field(&decoded, "booked_ns"), Value::Long(n) if *n > 0));
        assert_eq!(field(&decoded, "book_id"), &Value::Int(1));
        assert_eq!(field(&decoded, "instrument_id"), &Value::Int(1001));
        assert_eq!(
            field(&decoded, "symbol"),
            &Value::String("AAPL".to_string())
        );
        assert_eq!(field(&decoded, "side"), &Value::Enum(0, "BUY".to_string()));
        assert_eq!(field(&decoded, "qty_e2"), &Value::Long(80_000));
        assert_eq!(field(&decoded, "px_e9"), &Value::Long(150_000_000_000));
        assert_eq!(
            field(&decoded, "currency"),
            &Value::String("USD".to_string())
        );
        assert_eq!(
            field(&decoded, "trade_kind"),
            &Value::Enum(1, "INTERNAL_CROSS_LEG".to_string())
        );
        assert_eq!(
            field(&decoded, "cross_id"),
            &Value::Union(1, Box::new(Value::String(cross_id.to_string())))
        );
        assert_eq!(
            field(&decoded, "parent_cl_ord_id"),
            &Value::Union(0, Box::new(Value::Null))
        );
        assert_eq!(
            field(&decoded, "exec_id"),
            &Value::Union(0, Box::new(Value::Null))
        );
        assert_eq!(
            field(&decoded, "counterparty"),
            &Value::String("INTERNAL".to_string())
        );
    }

    #[test]
    fn encode_decode_trade_external_fill() {
        let schemas = Schemas::new().unwrap();
        let uni = test_universe();
        let cl_ord_id = ClOrdId::from_seq(42);
        let exec_id = ExecId::from_bytes(*b"00000000000000EXEC-1");
        let leg = TradeLeg {
            book: BookId(1),
            instrument: InstrumentId(1001),
            side: Side::Sell,
            qty_e2: 20_000,
            px_e9: 151_000_000_000,
            kind: TradeKind::ExternalFill,
            cross_id: None,
            parent_cl_ord_id: Some(cl_ord_id),
            exec_id: Some(exec_id),
            counterparty: "NYSE",
        };

        let bytes = schemas.encode(&PostTradeEvent::Trade(leg), &uni).unwrap();
        let decoded = from_avro_datum(&schemas.trade, &mut &bytes[..], None).unwrap();

        assert_eq!(field(&decoded, "side"), &Value::Enum(1, "SELL".to_string()));
        assert_eq!(
            field(&decoded, "trade_kind"),
            &Value::Enum(0, "EXTERNAL_FILL".to_string())
        );
        assert_eq!(
            field(&decoded, "cross_id"),
            &Value::Union(0, Box::new(Value::Null))
        );
        assert_eq!(
            field(&decoded, "parent_cl_ord_id"),
            &Value::Union(
                1,
                Box::new(Value::String("00000000000000000042".to_string()))
            )
        );
        assert_eq!(
            field(&decoded, "exec_id"),
            &Value::Union(
                1,
                Box::new(Value::String("00000000000000EXEC-1".to_string()))
            )
        );
        assert_eq!(
            field(&decoded, "counterparty"),
            &Value::String("NYSE".to_string())
        );
    }

    #[test]
    fn encode_decode_cross_cycle_and_direct() {
        let schemas = Schemas::new().unwrap();
        let uni = test_universe();
        let cross_id = Uuid::now_v7();
        let base = Cross {
            cross_id,
            instrument: InstrumentId(1001),
            buy_book: BookId(1),
            sell_book: BookId(2),
            qty_e2: 800_000,
            ref_px_e9: 150_000_000_000,
            policy_id: "ARRIVAL_MID",
            netting_cycle_id: NettingCycleId::Cycle(7),
        };

        let bytes = schemas.encode(&PostTradeEvent::Cross(base), &uni).unwrap();
        let decoded = from_avro_datum(&schemas.cross, &mut &bytes[..], None).unwrap();

        assert_valid_uuid(field(&decoded, "msg_id"));
        assert_eq!(
            field(&decoded, "cross_id"),
            &Value::String(cross_id.to_string())
        );
        assert!(matches!(field(&decoded, "booked_ns"), Value::Long(n) if *n > 0));
        assert_eq!(field(&decoded, "buy_book_id"), &Value::Int(1));
        assert_eq!(field(&decoded, "sell_book_id"), &Value::Int(2));
        assert_eq!(field(&decoded, "ref_px_e9"), &Value::Long(150_000_000_000));
        assert_eq!(
            field(&decoded, "px_policy_id"),
            &Value::String("ARRIVAL_MID".to_string())
        );
        assert_eq!(
            field(&decoded, "netting_cycle_id"),
            &Value::String("7".to_string())
        );

        let direct = Cross {
            netting_cycle_id: NettingCycleId::Direct,
            ..base
        };
        let bytes = schemas
            .encode(&PostTradeEvent::Cross(direct), &uni)
            .unwrap();
        let decoded = from_avro_datum(&schemas.cross, &mut &bytes[..], None).unwrap();
        assert_eq!(
            field(&decoded, "netting_cycle_id"),
            &Value::String("DIRECT".to_string())
        );
    }

    #[test]
    fn encode_decode_allocation() {
        let schemas = Schemas::new().unwrap();
        let uni = test_universe();
        let alloc = Allocation {
            parent_cl_ord_id: ClOrdId::from_seq(7),
            exec_id: ExecId::from_bytes(*b"00000000000000EXEC-9"),
            instrument: InstrumentId(1001),
            book: BookId(2),
            qty_e2: 40_000,
            px_e9: 150_500_000_000,
            netting_cycle_id: NettingCycleId::Cycle(3),
        };

        let bytes = schemas
            .encode(&PostTradeEvent::Allocation(alloc), &uni)
            .unwrap();
        let decoded = from_avro_datum(&schemas.allocation, &mut &bytes[..], None).unwrap();

        assert_eq!(
            field(&decoded, "parent_cl_ord_id"),
            &Value::String("00000000000000000007".to_string())
        );
        assert_eq!(
            field(&decoded, "exec_id"),
            &Value::String("00000000000000EXEC-9".to_string())
        );
        assert_eq!(field(&decoded, "book_id"), &Value::Int(2));
        assert_eq!(
            field(&decoded, "netting_cycle_id"),
            &Value::String("3".to_string())
        );
    }

    #[test]
    fn encode_decode_order_audit() {
        let schemas = Schemas::new().unwrap();
        let audit = OrderAudit {
            cl_ord_id: ClOrdId::from_seq(1),
            instrument: InstrumentId(1001),
            side: Side::Buy,
            from_status: OrderStatus::New,
            to_status: OrderStatus::PartiallyFilled,
            qty_e2: 10_000,
            cum_qty_e2: 10_000,
            leaves_qty_e2: 90_000,
            origin: AuditOrigin::NettingEngine,
            detail: None,
        };

        let bytes = schemas
            .encode(&PostTradeEvent::OrderAudit(audit), &test_universe())
            .unwrap();
        let decoded = from_avro_datum(&schemas.order_audit, &mut &bytes[..], None).unwrap();

        assert_valid_uuid(field(&decoded, "msg_id"));
        assert!(matches!(field(&decoded, "ts_ns"), Value::Long(n) if *n > 0));
        assert_eq!(field(&decoded, "side"), &Value::Enum(0, "BUY".to_string()));
        assert_eq!(
            field(&decoded, "transition"),
            &Value::String("NEW->PARTIALLY_FILLED".to_string())
        );
        assert_eq!(
            field(&decoded, "origin"),
            &Value::Enum(0, "NETTING_ENGINE".to_string())
        );
        assert_eq!(
            field(&decoded, "detail"),
            &Value::Union(0, Box::new(Value::Null))
        );
    }

    #[test]
    fn encode_unknown_instrument_errors() {
        let schemas = Schemas::new().unwrap();
        let uni = test_universe();
        let leg = TradeLeg {
            book: BookId(1),
            instrument: InstrumentId(9999), // absent from test_universe()
            side: Side::Buy,
            qty_e2: 1_000,
            px_e9: 100_000_000_000,
            kind: TradeKind::ExternalFill,
            cross_id: None,
            parent_cl_ord_id: Some(ClOrdId::from_seq(1)),
            exec_id: Some(ExecId::from_bytes([1; 20])),
            counterparty: "NYSE",
        };

        let err = schemas
            .encode(&PostTradeEvent::Trade(leg), &uni)
            .unwrap_err();
        assert!(matches!(
            err,
            PostTradeError::UnknownInstrument(InstrumentId(9999))
        ));
    }

    #[test]
    fn encode_id_overflow_errors() {
        let schemas = Schemas::new().unwrap();
        let uni = test_universe();
        let leg = TradeLeg {
            book: BookId(u32::MAX), // doesn't fit Avro `int` (i32)
            instrument: InstrumentId(1001),
            side: Side::Buy,
            qty_e2: 1_000,
            px_e9: 100_000_000_000,
            kind: TradeKind::ExternalFill,
            cross_id: None,
            parent_cl_ord_id: Some(ClOrdId::from_seq(1)),
            exec_id: Some(ExecId::from_bytes([1; 20])),
            counterparty: "NYSE",
        };

        let err = schemas
            .encode(&PostTradeEvent::Trade(leg), &uni)
            .unwrap_err();
        assert!(matches!(err, PostTradeError::IdOverflow(u32::MAX)));
    }
}
