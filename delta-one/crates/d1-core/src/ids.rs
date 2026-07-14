//! Newtype identifiers. Never pass bare `u32` across module boundaries
//! (delta-one/CLAUDE.md Rust guardrails).

/// Firm book identifier (`protocol/refdata/universe.json` `book_id`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct BookId(pub u32);

/// Canonical instrument identifier (`protocol/refdata/universe.json` `instrument_id`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct InstrumentId(pub u32);

/// Client order id: fixed-size so the order store never allocates per-order
/// (delta-one/CLAUDE.md hot-path rule #1). FIX `ClOrdID` *string* mapping is
/// a Slice 2 concern (`d1-gateway-fix`), not this type.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ClOrdId(pub [u8; 20]);

impl ClOrdId {
    /// Wrap raw bytes (e.g. a FIX `ClOrdID` once decoded).
    #[must_use]
    pub fn from_bytes(bytes: [u8; 20]) -> Self {
        Self(bytes)
    }

    /// Monotonic id from a sequence counter: right-aligned decimal ASCII,
    /// zero-padded. ponytail: caller owns the counter (no atomic generator
    /// here yet) — a real one lands with whichever gateway mints `ClOrdID`s
    /// (Slice 2).
    #[must_use]
    pub fn from_seq(seq: u64) -> Self {
        let mut bytes = [b'0'; 20];
        let mut n = seq;
        for slot in bytes.iter_mut().rev() {
            *slot = b'0' + (n % 10) as u8;
            n /= 10;
        }
        Self(bytes)
    }
}

/// Execution id: fixed-size, used to dedupe replayed `ExecutionReport`s
/// (root CLAUDE.md invariant #4 — idempotency).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ExecId(pub [u8; 20]);

impl ExecId {
    /// Wrap raw bytes (e.g. a FIX `ExecID` once decoded).
    #[must_use]
    pub fn from_bytes(bytes: [u8; 20]) -> Self {
        Self(bytes)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn from_seq_zero_pads_right_aligned() {
        assert_eq!(ClOrdId::from_seq(42).0, *b"00000000000000000042");
    }

    #[test]
    fn from_seq_distinguishes_sequential_ids() {
        assert_ne!(ClOrdId::from_seq(1), ClOrdId::from_seq(2));
    }
}
