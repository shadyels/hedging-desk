//! d1-core — market-data ingest and position keeping for the Delta One hot
//! path. See delta-one/CLAUDE.md for the crate's role and hot-path rules.

pub mod error;
pub mod feed;
pub mod ids;
pub mod keeper;
pub mod market_data;
pub mod order;
pub mod target;

pub use error::OrderError;
pub use feed::FeedTick;
pub use ids::{BookId, ClOrdId, ExecId, InstrumentId};
pub use keeper::{Position, PositionKeeper, Side};
pub use market_data::{MarketData, Quote};
pub use order::{ExecEvent, ExecReport, Fill, Order, OrderStatus, OrderStore};
pub use target::Target;
