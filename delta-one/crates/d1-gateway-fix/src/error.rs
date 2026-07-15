//! `d1-gateway-fix`'s error type (delta-one/CLAUDE.md: one `thiserror` enum per crate).

/// Failure modes for converting between `d1-core` types and FIX messages
/// (`crate::convert`), and for the FIX session machinery itself.
#[derive(Debug, thiserror::Error)]
pub enum FixError {
    /// A FIX `ExecID` (tag 17) is longer than the 20-byte `ExecId` newtype
    /// can hold without truncating (`d1-core::ids` — truncation risks
    /// colliding two distinct execs into one dedupe key).
    #[error("ExecID is {0} bytes, exceeds the 20-byte ExecId capacity")]
    ExecIdTooLong(usize),
    /// A FIX `ClOrdID` (tag 11) is longer than the 20-byte `ClOrdId` newtype
    /// can hold.
    #[error("ClOrdID is {0} bytes, exceeds the 20-byte ClOrdId capacity")]
    ClOrdIdTooLong(usize),
    /// A required FIX tag was absent from the message.
    #[error("missing required FIX tag {0}")]
    MissingField(i32),
    /// `OrdStatus` (tag 39) carried a value with no native `OrderStatus` mapping.
    #[error("unknown OrdStatus value: {0:?}")]
    UnknownStatus(String),
    /// A field value could not be parsed as the expected numeric/decimal shape.
    #[error("failed to parse FIX tag {tag}: {reason}")]
    Parse {
        /// Tag number that failed to parse.
        tag: i32,
        /// What went wrong.
        reason: String,
    },
    /// Underlying `quickfix` library failure (session/message-build errors).
    #[error(transparent)]
    Fix(#[from] quickfix::QuickFixError),
}
