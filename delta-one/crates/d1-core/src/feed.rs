//! Market-data feed tick: the sim -> d1-core ingest seam.
//!
//! ponytail: M1 ingest is a direct function call (`MarketData::ingest`). No
//! threads/rings exist yet because nothing needs decoupling until M2 adds the
//! NATS/FIX/Kafka gateways. M2 drops an SPSC ring in front of this call once a
//! real consumer thread exists (`rtrb` vs `crossbeam` ADR pending,
//! delta-one/CLAUDE.md).

use crate::ids::InstrumentId;

/// A single market-data update for one instrument. Plain struct, no Protobuf
/// (ADR-004: proto never enters `d1-core`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FeedTick {
    /// Instrument this tick is for.
    pub instrument_id: InstrumentId,
    /// Best bid price, fixed-point ×10⁹.
    pub bid_px_e9: i64,
    /// Best ask price, fixed-point ×10⁹.
    pub ask_px_e9: i64,
    /// Last/reference price, fixed-point ×10⁹.
    pub last_px_e9: i64,
    /// Exchange timestamp, nanoseconds since Unix epoch.
    pub exch_ts_ns: u64,
}
