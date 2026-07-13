//! Newtype identifiers. Never pass bare `u32` across module boundaries
//! (delta-one/CLAUDE.md Rust guardrails).

/// Firm book identifier (`protocol/refdata/universe.json` `book_id`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct BookId(pub u32);

/// Canonical instrument identifier (`protocol/refdata/universe.json` `instrument_id`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct InstrumentId(pub u32);
