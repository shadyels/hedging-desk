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
    /// here yet) — owner undecided.
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

/// FIX SOH. A FIX `String` value can never contain it (it is the field
/// delimiter; QuickFIX's `extractField` ends non-`data` values at it), so
/// padding with it cannot collide with any ExecID byte.
const EXEC_ID_PAD: u8 = 0x01;

impl ExecId {
    /// Wrap raw 20 bytes verbatim (e.g. a digest). FIX `ExecID` (tag 17) has
    /// no 20-byte cap; the FIX converter hashes oversize IDs rather than
    /// truncating, and encodes short ones with [`ExecId::from_short`] (see
    /// `exec_id_from_fix`).
    #[must_use]
    pub fn from_bytes(bytes: [u8; 20]) -> Self {
        Self(bytes)
    }

    /// Encode an ID of at most 20 bytes, right-aligned and left-padded with
    /// 0x01. Injective over inputs that contain no 0x01. `None` if longer
    /// than 20 bytes. Distinct from hashed (`from_bytes(digest)`) ids only
    /// probabilistically: a digest could in principle equal a padded short id.
    #[must_use]
    pub fn from_short(id: &[u8]) -> Option<Self> {
        let mut buf = [EXEC_ID_PAD; 20];
        buf.get_mut(20usize.checked_sub(id.len())?..)?
            .copy_from_slice(id);
        Some(Self(buf))
    }

    /// Leading padding stripped: the original ID for `from_short` values;
    /// the raw digest bytes for a hashed (oversize) id.
    #[must_use]
    pub fn unpadded(&self) -> &[u8] {
        let start = self.0.iter().position(|&b| b != EXEC_ID_PAD).unwrap_or(20);
        self.0.get(start..).unwrap_or(&[])
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::indexing_slicing)] // tests: hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::*;
    use proptest::strategy::Strategy;

    #[test]
    fn from_seq_zero_pads_right_aligned() {
        assert_eq!(ClOrdId::from_seq(42).0, *b"00000000000000000042");
    }

    #[test]
    fn from_seq_distinguishes_sequential_ids() {
        assert_ne!(ClOrdId::from_seq(1), ClOrdId::from_seq(2));
    }

    #[test]
    fn exec_id_from_short_rejects_oversize() {
        assert!(ExecId::from_short(&[b'x'; 21]).is_none());
        assert!(ExecId::from_short(&[b'x'; 20]).is_some());
    }

    proptest::proptest! {
        #[test]
        fn exec_id_short_encoding_round_trips(
            v in proptest::collection::vec(
                proptest::num::u8::ANY.prop_filter("no SOH", |b| *b != 0x01),
                0..=20,
            )
        ) {
            let id = ExecId::from_short(&v).unwrap();
            proptest::prop_assert_eq!(id.unpadded(), &v[..]);
        }
    }
}
