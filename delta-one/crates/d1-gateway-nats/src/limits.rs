//! Tier-1 limit aggregation (ADR-008, ADR-014 §3): per-underlying firm
//! vega / gamma / rho from EXO's `ValuationSnapshot`s, checked against the
//! `d1.toml [limits.defaults]` caps. Pure state machine with an injected
//! `Instant` -- no NATS, no clock reads -- and it only ever runs on the
//! gateway thread, never on the core thread or the hot path.
//!
//! Fail direction (ADR-014 §3): every path on which a book's Greeks cannot be
//! trusted or seen (rejected snapshot, no snapshot within
//! `valuation_max_age_s`) degrades TOWARD alerting: the affected underlyings
//! are held at least WARN with the reason in `text`. A forged or broken feed
//! can therefore raise alerts, but data going quiet never clears one. (A
//! forged feed can still suppress alerts with zeroed lines: ADR-014 §13.)

use std::collections::{BTreeMap, BTreeSet};
use std::time::{Duration, Instant};

use d1_core::{BookId, InstrumentId};

/// Most lines one accepted `ValuationSnapshot` may carry (ADR-014 §14).
pub const MAX_LINES: usize = 1024;
/// Most `flagged_greeks` one accepted line may carry (ADR-014 §14).
pub const MAX_FLAGGED_PER_LINE: usize = 8;
/// Alert `text` is display data: cap it (ADR-014 §14).
const MAX_TEXT_BYTES: usize = 256;

/// The six Tier-1 caps (`d1.toml [limits.defaults]`). Validated by
/// `crates/d1/src/config.rs` (`> 0`, `warn < hard`); not re-validated here.
#[derive(Debug, Clone, Copy)]
pub struct TierOneLimits {
    /// Vega warn, ccy x 1e9 per vol point.
    pub vega_warn_e9: i64,
    /// Vega hard, ccy x 1e9 per vol point.
    pub vega_hard_e9: i64,
    /// Gamma warn, dDelta per +1% spot, qty_e2.
    pub gamma_warn_qty_e2: i64,
    /// Gamma hard, dDelta per +1% spot, qty_e2.
    pub gamma_hard_qty_e2: i64,
    /// Rho warn, ccy x 1e9 per bp.
    pub rho_warn_e9: i64,
    /// Rho hard, ccy x 1e9 per bp.
    pub rho_hard_e9: i64,
}

impl TierOneLimits {
    /// `(warn, hard)` for one Greek.
    fn pair(&self, greek: Greek) -> (i64, i64) {
        match greek {
            Greek::Vega => (self.vega_warn_e9, self.vega_hard_e9),
            Greek::Gamma => (self.gamma_warn_qty_e2, self.gamma_hard_qty_e2),
            Greek::Rho => (self.rho_warn_e9, self.rho_hard_e9),
        }
    }
}

/// A limited Greek.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Greek {
    /// ccy x 1e9 per vol point.
    Vega,
    /// dDelta per +1% spot, qty_e2.
    Gamma,
    /// ccy x 1e9 per bp.
    Rho,
}

impl Greek {
    fn name(self) -> &'static str {
        match self {
            Self::Vega => "vega",
            Self::Gamma => "gamma",
            Self::Rho => "rho",
        }
    }

    fn unit(self) -> &'static str {
        match self {
            Self::Vega => "ccy/vol-pt",
            Self::Gamma => "qty dDelta/+1% spot",
            Self::Rho => "ccy/bp",
        }
    }
}

/// Where an exposure sits relative to its caps. Ordered `Ok < Warn < Hard`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Level {
    /// Inside the warn cap.
    Ok,
    /// At or beyond warn, inside hard.
    Warn,
    /// At or beyond hard.
    Hard,
}

/// One book's valuation feed as seen at a given instant (ADR-014 §3), for
/// `d1.health`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ValuationState {
    /// Nothing was ever received for the book (neither accepted nor rejected).
    None,
    /// Last snapshot accepted and fresher than `max_age`.
    Ok,
    /// Last accepted snapshot is older than `max_age`.
    Stale,
    /// The latest snapshot was rejected; no accept since.
    Invalid,
}

/// Level of an absolute exposure: `abs >= hard` is `Hard`, `abs >= warn` is
/// `Warn`. Takes `u128` (the `unsigned_abs` of an `i128` sum), so there is no
/// `abs()` on `i64` anywhere on this path (`i64::MIN` is representable).
#[must_use]
pub fn level(abs: u128, warn: i64, hard: i64) -> Level {
    if abs >= u128::from(hard.unsigned_abs()) {
        Level::Hard
    } else if abs >= u128::from(warn.unsigned_abs()) {
        Level::Warn
    } else {
        Level::Ok
    }
}

/// One `ValuationSnapshot` line reduced to what the limits read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ValLine {
    /// `None` if the wire line had no `underlying` (rejected: rule 3).
    pub underlying: Option<InstrumentId>,
    /// Desk-position vega, ccy x 1e9 per vol point.
    pub vega_e9: i64,
    /// Desk-position gamma, qty_e2 per +1% spot.
    pub gamma_qty_e2: i64,
    /// Desk-position rho, ccy x 1e9 per bp.
    pub rho_e9: i64,
    /// Desk-position delta, units of underlying x 1e2 (ADR-014 §9).
    pub delta_qty_e2: i64,
    /// Desk-position theta, ccy x 1e9 per calendar day.
    pub theta_e9: i64,
    /// Desk-position dividend sensitivity, ccy x 1e9 per +1% yield.
    pub div_sens_e9: i64,
    /// `flagged_greeks.len()`.
    pub n_flagged: usize,
}

/// A `ValuationSnapshot` reduced to what acceptance and aggregation read
/// (`convert::valuation_from_pb`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Valuation {
    /// Payload `book_id`.
    pub book: BookId,
    /// Epoch-UTC wall clock at EXO's revaluation start.
    pub as_of_ns: u64,
    /// `valuation` present with non-empty `model_id`, `params_hash`, `git_sha`.
    pub meta_complete: bool,
    /// Product lines.
    pub lines: Vec<ValLine>,
}

/// One level change, ready for `convert::risk_limit_alert_to_pb`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlertEvent {
    /// Underlying the exposure is on.
    pub underlying: InstrumentId,
    /// Which Greek.
    pub greek: Greek,
    /// The NEW level.
    pub level: Level,
    /// The firm exposure, saturated to `i64` by sign if the sum does not fit.
    pub value: i64,
    /// The cap crossed: hard for `Hard`, otherwise warn.
    pub limit: i64,
    /// Human-readable, with units. Display only, at most 256 bytes.
    pub text: String,
}

#[derive(Debug, Clone, Copy)]
struct AcceptedLine {
    underlying: InstrumentId,
    vega_e9: i64,
    gamma_qty_e2: i64,
    rho_e9: i64,
    delta_qty_e2: i64,
    theta_e9: i64,
    div_sens_e9: i64,
    /// `n_flagged > 0`: some MC std error breached its configured max.
    flagged: bool,
}

#[derive(Debug, Default)]
struct BookEntry {
    /// Last ACCEPTED lines; kept while invalid or stale (dropping them would
    /// under-report exposure).
    lines: Vec<AcceptedLine>,
    /// Last accepted `as_of_ns`.
    as_of_ns: Option<u64>,
    /// D1-local receipt time of the last accepted snapshot.
    received: Option<Instant>,
    /// `Some(reason)` after an attributable rejection, until the next accept.
    invalid: Option<String>,
    rejects: u64,
}

/// Per-book valuation state and per-(underlying, Greek) alert levels.
///
/// ponytail: no hysteresis -- alerts can flap at MC noise near a limit. Add a
/// clear-below-warn-minus-margin rule on the first observed flapping or a
/// book decommission (ADR-014 §3). Books that never published raise no alert
/// (Delta One cannot know which books EXO values); `d1.health` now surfaces
/// them as `valuation=none`, and surfaces reject counters (`health::health_detail`).
///
/// ponytail: D1-held listed options contribute no vega (Delta One holds
/// linear instruments only in P2 and cannot price options). Owner P4.M3.
#[derive(Debug)]
pub struct LimitBook {
    limits: TierOneLimits,
    max_age: Duration,
    books: BTreeSet<BookId>,
    instruments: BTreeSet<InstrumentId>,
    state: BTreeMap<BookId, BookEntry>,
    /// Only non-`Ok` levels are stored.
    levels: BTreeMap<(InstrumentId, Greek), Level>,
    unattributable_rejects: u64,
}

/// Aggregation scratch for one (underlying, Greek).
#[derive(Default)]
struct Acc {
    sum: i128,
    overflow: bool,
    forced: Vec<String>,
    forced_books: Vec<BookId>,
}

/// Aggregation scratch for one underlying: the three limited Greeks plus the
/// monitored-only sums and the flagged bit. Produced by `LimitBook::aggregate`,
/// the only summation path (`evaluate` and `firm_greeks` both read it).
#[derive(Default)]
struct UnderAgg {
    vega: Acc,
    gamma: Acc,
    rho: Acc,
    delta_qty_e2: i128,
    theta_e9: i128,
    div_sens_e9: i128,
    flagged: bool,
}

impl UnderAgg {
    fn acc(&self, greek: Greek) -> &Acc {
        match greek {
            Greek::Vega => &self.vega,
            Greek::Gamma => &self.gamma,
            Greek::Rho => &self.rho,
        }
    }

    fn acc_mut(&mut self, greek: Greek) -> &mut Acc {
        match greek {
            Greek::Vega => &mut self.vega,
            Greek::Gamma => &mut self.gamma,
            Greek::Rho => &mut self.rho,
        }
    }

    /// A contributing book is stale or invalid.
    fn suspect(&self) -> bool {
        [Greek::Vega, Greek::Gamma, Greek::Rho]
            .iter()
            .any(|&g| !self.acc(g).forced.is_empty())
    }
}

/// One underlying's firm-wide EXO-side exposure, for `RiskSnapshot`
/// (`convert::risk_snapshot_to_pb`). Sums are `i128` (saturated by sign only
/// at the wire); levels are what the alerts last reported for the key.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FirmGreeks {
    /// The underlying.
    pub underlying: InstrumentId,
    /// Σ accepted EXO `delta_qty_e2` across books, units x 1e2.
    pub exo_delta_qty_e2: i128,
    /// Σ vega, ccy x 1e9 per vol point.
    pub vega_e9: i128,
    /// Σ gamma, qty_e2 per +1% spot.
    pub gamma_qty_e2: i128,
    /// Σ rho, ccy x 1e9 per bp.
    pub rho_e9: i128,
    /// Σ theta, ccy x 1e9 per calendar day.
    pub theta_e9: i128,
    /// Σ dividend sensitivity, ccy x 1e9 per +1% yield.
    pub div_sens_e9: i128,
    /// Some contributing line had `flagged_greeks`.
    pub flagged: bool,
    /// Vega level as last alerted (absent = OK).
    pub vega: Level,
    /// Gamma level as last alerted.
    pub gamma: Level,
    /// Rho level as last alerted.
    pub rho: Level,
    /// A contributing book is stale or invalid (ADR-014 §3).
    pub suspect: bool,
}

/// `v` as `i64`, saturated by sign if it does not fit.
#[must_use]
pub(crate) fn saturate_i64(v: i128) -> i64 {
    i64::try_from(v).unwrap_or(if v < 0 { i64::MIN } else { i64::MAX })
}

impl LimitBook {
    /// `books`/`instruments` are the refdata universe; `max_age` is
    /// `d1.toml [limits] valuation_max_age_s`.
    #[must_use]
    pub fn new(
        limits: TierOneLimits,
        max_age: Duration,
        books: &[BookId],
        instruments: &[InstrumentId],
    ) -> Self {
        Self {
            limits,
            max_age,
            books: books.iter().copied().collect(),
            instruments: instruments.iter().copied().collect(),
            state: BTreeMap::new(),
            levels: BTreeMap::new(),
            unattributable_rejects: 0,
        }
    }

    /// Total rejections that could not be attributed to a book (undecodable
    /// payload, subject/payload book mismatch, unknown book).
    #[must_use]
    pub fn unattributable_rejects(&self) -> u64 {
        self.unattributable_rejects
    }

    /// Rejections attributed to `book`.
    #[must_use]
    pub fn rejects(&self, book: BookId) -> u64 {
        self.state.get(&book).map_or(0, |e| e.rejects)
    }

    /// `book`'s valuation feed at `now`. Reuses `forced_reason` for the stale
    /// test, so staleness has one definition. A book rejected on its first-ever
    /// snapshot reads `Invalid`; one never heard from reads `None`.
    #[must_use]
    pub fn valuation_state(&self, book: BookId, now: Instant) -> ValuationState {
        let Some(entry) = self.state.get(&book) else {
            return ValuationState::None;
        };
        if entry.invalid.is_some() {
            ValuationState::Invalid
        } else if entry.received.is_none() {
            ValuationState::None
        } else if self.forced_reason(book, entry, now).is_some() {
            ValuationState::Stale
        } else {
            ValuationState::Ok
        }
    }

    /// One line for the shutdown log: every non-zero reject counter.
    #[must_use]
    pub fn reject_summary(&self) -> String {
        let mut out = format!("unattributable={}", self.unattributable_rejects);
        for (book, entry) in &self.state {
            out.push_str(&format!(" book{}={}", book.0, entry.rejects));
        }
        out
    }

    /// Count and log a rejection that has no book to hold responsible (for
    /// example an undecodable payload).
    pub fn reject_unattributable(&mut self, reason: &str) {
        self.unattributable_rejects = self.unattributable_rejects.saturating_add(1);
        eprintln!(
            "nats: valuation rejected (unattributable): {:?}",
            truncated(reason)
        );
    }

    /// Offer one decoded snapshot received on `exo.valuations.<subject_book>`
    /// (`None` if the subject token did not parse). Returns the level changes
    /// it causes.
    ///
    /// Acceptance rules, in order (ADR-014 §3): 1. subject token == payload
    /// `book_id` and the book is in the universe (else unattributable: log and
    /// count only); 2. `ValuationMeta` complete; 3. at most 1,024 lines, every
    /// underlying in the universe, at most 8 `flagged_greeks` per line;
    /// 4. `as_of_ns` non-decreasing. A rule 2-4 failure makes the book
    /// invalid: its last accepted lines stay in the aggregate and their
    /// underlyings are held at least WARN until the next accepted snapshot.
    pub fn on_snapshot(
        &mut self,
        subject_book: Option<BookId>,
        snap: &Valuation,
        now: Instant,
    ) -> Vec<AlertEvent> {
        if subject_book != Some(snap.book) {
            self.reject_unattributable(&format!(
                "subject book {subject_book:?} != payload book {:?}",
                snap.book
            ));
            return Vec::new();
        }
        if !self.books.contains(&snap.book) {
            self.reject_unattributable(&format!("book {:?} not in universe", snap.book));
            return Vec::new();
        }

        let last_as_of = self.state.get(&snap.book).and_then(|e| e.as_of_ns);
        match self.check(snap, last_as_of) {
            Ok(lines) => {
                let entry = self.state.entry(snap.book).or_default();
                entry.lines = lines;
                entry.as_of_ns = Some(snap.as_of_ns);
                entry.received = Some(now);
                entry.invalid = None;
            }
            Err(reason) => {
                eprintln!(
                    "nats: valuation rejected book={:?} reason={:?}",
                    snap.book,
                    truncated(&reason)
                );
                let entry = self.state.entry(snap.book).or_default();
                entry.rejects = entry.rejects.saturating_add(1);
                entry.invalid = Some(reason);
            }
        }
        self.evaluate(now)
    }

    /// Re-evaluate against the clock: a book with no accepted snapshot within
    /// `max_age` is stale and its underlyings are held at least WARN. Run from
    /// the gateway's 1 s interval.
    pub fn tick(&mut self, now: Instant) -> Vec<AlertEvent> {
        self.evaluate(now)
    }

    /// Rules 2-4 (rule 1 is checked by the caller). `Ok` carries the lines
    /// narrowed to what the aggregate reads.
    fn check(
        &self,
        snap: &Valuation,
        last_as_of: Option<u64>,
    ) -> Result<Vec<AcceptedLine>, String> {
        if !snap.meta_complete {
            return Err("ValuationMeta missing or incomplete".to_string());
        }
        if snap.lines.len() > MAX_LINES {
            return Err(format!("{} lines > cap {MAX_LINES}", snap.lines.len()));
        }
        let mut lines = Vec::with_capacity(snap.lines.len());
        for line in &snap.lines {
            let Some(underlying) = line.underlying.filter(|u| self.instruments.contains(u)) else {
                return Err("line underlying missing or not in universe".to_string());
            };
            if line.n_flagged > MAX_FLAGGED_PER_LINE {
                return Err(format!(
                    "{} flagged_greeks on a line > cap {MAX_FLAGGED_PER_LINE}",
                    line.n_flagged
                ));
            }
            lines.push(AcceptedLine {
                underlying,
                vega_e9: line.vega_e9,
                gamma_qty_e2: line.gamma_qty_e2,
                rho_e9: line.rho_e9,
                delta_qty_e2: line.delta_qty_e2,
                theta_e9: line.theta_e9,
                div_sens_e9: line.div_sens_e9,
                flagged: line.n_flagged > 0,
            });
        }
        // Non-decreasing, not strictly increasing: EXO's interval republish
        // repeats the same `as_of_ns` under a fresh msg_id.
        if last_as_of.is_some_and(|last| snap.as_of_ns < last) {
            return Err(format!(
                "as_of_ns {} older than last accepted {}",
                snap.as_of_ns,
                last_as_of.unwrap_or(0)
            ));
        }
        Ok(lines)
    }

    /// Why `entry`'s contribution is suspect, if it is.
    fn forced_reason(&self, book: BookId, entry: &BookEntry, now: Instant) -> Option<String> {
        if let Some(reason) = &entry.invalid {
            return Some(format!(
                "book {}: rejected snapshot ({reason}); evaluated on last accepted data",
                book.0
            ));
        }
        let age = now.saturating_duration_since(entry.received?);
        (age > self.max_age).then(|| {
            format!(
                "book {}: stale (no valuation for {} s)",
                book.0,
                age.as_secs()
            )
        })
    }

    /// The ONE summation path: every book's last accepted lines per underlying
    /// in `i128`, with the books that make each underlying suspect at `now`.
    /// A key that left the aggregate (replaced snapshot dropped the
    /// underlying) is back at sum 0 and must still be able to clear, so every
    /// underlying with a stored level is present.
    fn aggregate(&self, now: Instant) -> BTreeMap<InstrumentId, UnderAgg> {
        let mut agg: BTreeMap<InstrumentId, UnderAgg> = BTreeMap::new();
        for (&book, entry) in &self.state {
            let forced = self.forced_reason(book, entry, now);
            for line in &entry.lines {
                let under = agg.entry(line.underlying).or_default();
                under.delta_qty_e2 += i128::from(line.delta_qty_e2);
                under.theta_e9 += i128::from(line.theta_e9);
                under.div_sens_e9 += i128::from(line.div_sens_e9);
                under.flagged |= line.flagged;
                for (greek, v) in [
                    (Greek::Vega, line.vega_e9),
                    (Greek::Gamma, line.gamma_qty_e2),
                    (Greek::Rho, line.rho_e9),
                ] {
                    let acc = under.acc_mut(greek);
                    match acc.sum.checked_add(i128::from(v)) {
                        Some(s) => acc.sum = s,
                        None => acc.overflow = true,
                    }
                    if let Some(reason) = &forced {
                        if !acc.forced_books.contains(&book) {
                            acc.forced_books.push(book);
                            acc.forced.push(reason.clone());
                        }
                    }
                }
            }
        }
        for (underlying, _) in self.levels.keys() {
            agg.entry(*underlying).or_default();
        }
        agg
    }

    /// Firm-wide EXO-side exposure per underlying at `now`, for the 1 s
    /// `RiskSnapshot`. Read-only: levels are those the alerts last reported
    /// (call after `tick`).
    #[must_use]
    pub fn firm_greeks(&self, now: Instant) -> BTreeMap<InstrumentId, FirmGreeks> {
        let level_of = |u, g| self.levels.get(&(u, g)).copied().unwrap_or(Level::Ok);
        self.aggregate(now)
            .into_iter()
            .map(|(u, a)| {
                let row = FirmGreeks {
                    underlying: u,
                    exo_delta_qty_e2: a.delta_qty_e2,
                    vega_e9: a.vega.sum,
                    gamma_qty_e2: a.gamma.sum,
                    rho_e9: a.rho.sum,
                    theta_e9: a.theta_e9,
                    div_sens_e9: a.div_sens_e9,
                    flagged: a.flagged,
                    vega: level_of(u, Greek::Vega),
                    gamma: level_of(u, Greek::Gamma),
                    rho: level_of(u, Greek::Rho),
                    suspect: a.suspect(),
                };
                (u, row)
            })
            .collect()
    }

    /// The configured caps (for the `RiskSnapshot` threshold fields).
    #[must_use]
    pub fn limits(&self) -> TierOneLimits {
        self.limits
    }

    /// Level every aggregated (underlying, Greek) and emit one event per level
    /// CHANGE.
    fn evaluate(&mut self, now: Instant) -> Vec<AlertEvent> {
        let agg = self.aggregate(now);

        let mut events = Vec::new();
        for (underlying, under) in &agg {
            let underlying = *underlying;
            for greek in [Greek::Vega, Greek::Gamma, Greek::Rho] {
                let acc = under.acc(greek);
                let (warn, hard) = self.limits.pair(greek);
                let value = i64::try_from(acc.sum).ok();
                let natural = if acc.overflow || value.is_none() {
                    Level::Hard
                } else {
                    level(acc.sum.unsigned_abs(), warn, hard)
                };
                let lvl = if natural == Level::Ok && !acc.forced.is_empty() {
                    Level::Warn
                } else {
                    natural
                };
                let prev = self
                    .levels
                    .get(&(underlying, greek))
                    .copied()
                    .unwrap_or(Level::Ok);
                if lvl == prev {
                    continue;
                }
                if lvl == Level::Ok {
                    self.levels.remove(&(underlying, greek));
                } else {
                    self.levels.insert((underlying, greek), lvl);
                }
                let saturated = saturate_i64(acc.sum);
                let limit = if lvl == Level::Hard { hard } else { warn };
                let mut text = event_text(underlying, greek, natural, acc, saturated, (warn, hard));
                for reason in &acc.forced {
                    text.push_str("; ");
                    text.push_str(reason);
                }
                events.push(AlertEvent {
                    underlying,
                    greek,
                    level: lvl,
                    value: saturated,
                    limit,
                    text: truncated(&text).to_string(),
                });
            }
        }
        events
    }
}

fn event_text(
    underlying: InstrumentId,
    greek: Greek,
    natural: Level,
    acc: &Acc,
    value: i64,
    (warn, hard): (i64, i64),
) -> String {
    let fmt = |v: i64| match greek {
        Greek::Gamma => fmt_scaled(i128::from(v), 100),
        Greek::Vega | Greek::Rho => fmt_scaled(i128::from(v), 1_000_000_000),
    };
    let head = format!("instrument {} {}", underlying.0, greek.name());
    if acc.overflow || i64::try_from(acc.sum).is_err() {
        return format!("{head}: overflow (firm sum exceeds i64)");
    }
    let unit = greek.unit();
    match natural {
        Level::Hard => format!("{head} {} {unit} >= hard {}", fmt(value), fmt(hard)),
        Level::Warn => format!("{head} {} {unit} >= warn {}", fmt(value), fmt(warn)),
        Level::Ok if acc.forced.is_empty() => {
            format!(
                "{head} {} {unit}: back inside warn {}",
                fmt(value),
                fmt(warn)
            )
        }
        Level::Ok => format!("{head} {} {unit}: held at warn on suspect data", fmt(value)),
    }
}

/// `v / scale` to two decimals, truncated, integer arithmetic only (money is
/// never a float). `scale` is 100 (`_e2`) or 1e9 (`_e9`).
fn fmt_scaled(v: i128, scale: u128) -> String {
    let abs = v.unsigned_abs();
    let whole = abs / scale;
    let cents = (abs % scale) * 100 / scale;
    let sign = if v < 0 { "-" } else { "" };
    format!("{sign}{whole}.{cents:02}")
}

/// `s` cut to at most 256 bytes on a UTF-8 boundary.
pub(crate) fn truncated(s: &str) -> &str {
    if s.len() <= MAX_TEXT_BYTES {
        return s;
    }
    let mut end = MAX_TEXT_BYTES;
    while !s.is_char_boundary(end) {
        end -= 1;
    }
    s.get(..end).unwrap_or("")
}

#[cfg(test)]
#[allow(clippy::unwrap_used)] // tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::*;

    const AAPL: InstrumentId = InstrumentId(1001);
    const MSFT: InstrumentId = InstrumentId(1002);
    const MAX_AGE: Duration = Duration::from_secs(15);

    fn limits() -> TierOneLimits {
        TierOneLimits {
            vega_warn_e9: 50,
            vega_hard_e9: 100,
            gamma_warn_qty_e2: 20,
            gamma_hard_qty_e2: 50,
            rho_warn_e9: 10,
            rho_hard_e9: 25,
        }
    }

    fn lb() -> LimitBook {
        LimitBook::new(limits(), MAX_AGE, &[BookId(3), BookId(4)], &[AAPL, MSFT])
    }

    fn line(u: InstrumentId, vega: i64, gamma: i64, rho: i64) -> ValLine {
        ValLine {
            underlying: Some(u),
            vega_e9: vega,
            gamma_qty_e2: gamma,
            rho_e9: rho,
            delta_qty_e2: 0,
            theta_e9: 0,
            div_sens_e9: 0,
            n_flagged: 0,
        }
    }

    fn snap(book: u32, as_of_ns: u64, lines: Vec<ValLine>) -> Valuation {
        Valuation {
            book: BookId(book),
            as_of_ns,
            meta_complete: true,
            lines,
        }
    }

    fn vega_events(events: &[AlertEvent]) -> Vec<&AlertEvent> {
        events.iter().filter(|e| e.greek == Greek::Vega).collect()
    }

    #[test]
    fn level_boundaries_are_inclusive() {
        assert_eq!(level(49, 50, 100), Level::Ok);
        assert_eq!(level(50, 50, 100), Level::Warn);
        assert_eq!(level(99, 50, 100), Level::Warn);
        assert_eq!(level(100, 50, 100), Level::Hard);
    }

    #[test]
    fn negative_exposure_levels_like_positive() {
        let mut b = lb();
        let t = Instant::now();
        let ev = b.on_snapshot(Some(BookId(3)), &snap(3, 1, vec![line(AAPL, -50, 0, 0)]), t);
        assert_eq!(ev.len(), 1);
        assert_eq!(ev.first().unwrap().level, Level::Warn);
        assert_eq!(ev.first().unwrap().value, -50);
        let ev = b.on_snapshot(
            Some(BookId(3)),
            &snap(3, 2, vec![line(AAPL, -100, 0, 0)]),
            t,
        );
        assert_eq!(ev.first().unwrap().level, Level::Hard);
    }

    #[test]
    fn warn_hard_clear_sequence_emits_three_alerts_and_no_change_emits_none() {
        let mut b = lb();
        let t = Instant::now();
        let mut all = Vec::new();
        for (as_of, vega) in [
            (1, 10),
            (2, 60),
            (3, 60),
            (4, 120),
            (5, 120),
            (6, 0),
            (7, 0),
        ] {
            let ev = b.on_snapshot(
                Some(BookId(3)),
                &snap(3, as_of, vec![line(AAPL, vega, 0, 0)]),
                t,
            );
            all.extend(ev);
        }
        assert_eq!(all.len(), 3, "{all:?}");
        let levels: Vec<Level> = all.iter().map(|e| e.level).collect();
        assert_eq!(levels, vec![Level::Warn, Level::Hard, Level::Ok]);
        assert_eq!(all.first().unwrap().limit, 50);
        assert_eq!(all.get(1).unwrap().limit, 100);
    }

    #[test]
    fn gamma_and_rho_use_their_own_limits() {
        let mut b = lb();
        let ev = b.on_snapshot(
            Some(BookId(3)),
            &snap(3, 1, vec![line(AAPL, 0, 25, 30)]),
            Instant::now(),
        );
        assert_eq!(ev.len(), 2);
        let gamma = ev.iter().find(|e| e.greek == Greek::Gamma).unwrap();
        assert_eq!((gamma.level, gamma.limit), (Level::Warn, 20));
        assert!(gamma.text.contains("0.25"), "{}", gamma.text);
        let rho = ev.iter().find(|e| e.greek == Greek::Rho).unwrap();
        assert_eq!((rho.level, rho.limit), (Level::Hard, 25));
    }

    #[test]
    fn two_books_on_one_underlying_sum_and_replacement_does_not_add() {
        let mut b = lb();
        let t = Instant::now();
        assert!(
            b.on_snapshot(Some(BookId(3)), &snap(3, 1, vec![line(AAPL, 30, 0, 0)]), t)
                .is_empty()
        );
        // 30 + 30 = 60 >= warn
        let ev = b.on_snapshot(Some(BookId(4)), &snap(4, 1, vec![line(AAPL, 30, 0, 0)]), t);
        assert_eq!(vega_events(&ev).first().unwrap().value, 60);
        // Replace book 3 (30 -> 10): sum 40, back inside warn. If it added,
        // it would stay at 70.
        let ev = b.on_snapshot(Some(BookId(3)), &snap(3, 2, vec![line(AAPL, 10, 0, 0)]), t);
        assert_eq!(ev.len(), 1);
        assert_eq!(ev.first().unwrap().level, Level::Ok);
    }

    #[test]
    fn offsetting_books_net_to_ok() {
        let mut b = lb();
        let t = Instant::now();
        b.on_snapshot(Some(BookId(3)), &snap(3, 1, vec![line(AAPL, 80, 0, 0)]), t);
        let ev = b.on_snapshot(Some(BookId(4)), &snap(4, 1, vec![line(AAPL, -80, 0, 0)]), t);
        assert_eq!(ev.first().unwrap().level, Level::Ok); // 80 -> Warn earlier, now 0
    }

    #[test]
    fn replacement_dropping_an_underlying_clears_it() {
        let mut b = lb();
        let t = Instant::now();
        b.on_snapshot(Some(BookId(3)), &snap(3, 1, vec![line(AAPL, 120, 0, 0)]), t);
        let ev = b.on_snapshot(Some(BookId(3)), &snap(3, 2, vec![line(MSFT, 1, 0, 0)]), t);
        assert_eq!(ev.len(), 1);
        assert_eq!(
            (ev.first().unwrap().underlying, ev.first().unwrap().level),
            (AAPL, Level::Ok)
        );
    }

    #[test]
    fn l_rej1_subject_payload_mismatch_is_unattributable_and_inert() {
        let mut b = lb();
        let ev = b.on_snapshot(
            Some(BookId(3)),
            &snap(4, 1, vec![line(AAPL, 500, 0, 0)]),
            Instant::now(),
        );
        assert!(ev.is_empty());
        assert_eq!(b.unattributable_rejects(), 1);
        assert_eq!(b.rejects(BookId(3)), 0);
        assert_eq!(b.rejects(BookId(4)), 0);
        // Unparseable subject and unknown book are the same class.
        assert!(
            b.on_snapshot(None, &snap(3, 1, vec![]), Instant::now())
                .is_empty()
        );
        assert!(
            b.on_snapshot(Some(BookId(9)), &snap(9, 1, vec![]), Instant::now())
                .is_empty()
        );
        assert_eq!(b.unattributable_rejects(), 3);
        // No state was created: a later valid snapshot is the first.
        assert!(b.tick(Instant::now()).is_empty());
    }

    #[test]
    fn l_rej2_missing_meta_after_accept_forces_warn_and_next_valid_clears() {
        let mut b = lb();
        let t = Instant::now();
        b.on_snapshot(Some(BookId(3)), &snap(3, 1, vec![line(AAPL, 10, 0, 0)]), t);
        let mut bad = snap(3, 2, vec![line(AAPL, 0, 0, 0)]);
        bad.meta_complete = false;
        let ev = b.on_snapshot(Some(BookId(3)), &bad, t);
        // vega, gamma and rho for AAPL all held at WARN.
        assert_eq!(ev.len(), 3);
        assert!(ev.iter().all(|e| e.level == Level::Warn));
        assert!(
            ev.iter().all(|e| e.text.contains("rejected snapshot")),
            "{ev:?}"
        );
        assert_eq!(b.rejects(BookId(3)), 1);
        // Last accepted VALUES are kept (vega 10, not the rejected 0).
        assert_eq!(vega_events(&ev).first().unwrap().value, 10);
        // Still invalid: no repeat alert.
        assert!(b.tick(t).is_empty());
        // Next valid snapshot recomputes; OK emits cleared.
        let ev = b.on_snapshot(Some(BookId(3)), &snap(3, 3, vec![line(AAPL, 10, 0, 0)]), t);
        assert_eq!(ev.len(), 3);
        assert!(ev.iter().all(|e| e.level == Level::Ok));
    }

    #[test]
    fn l_ord_older_rejected_equal_accepted() {
        let mut b = lb();
        let t = Instant::now();
        b.on_snapshot(
            Some(BookId(3)),
            &snap(3, 100, vec![line(AAPL, 10, 0, 0)]),
            t,
        );
        // Equal as_of_ns (EXO's interval republish) is accepted.
        let ev = b.on_snapshot(
            Some(BookId(3)),
            &snap(3, 100, vec![line(AAPL, 10, 0, 0)]),
            t,
        );
        assert!(ev.is_empty());
        assert_eq!(b.rejects(BookId(3)), 0);
        // Strictly older is a replay: rejected, book invalid, WARN-forced.
        let ev = b.on_snapshot(Some(BookId(3)), &snap(3, 99, vec![line(AAPL, 0, 0, 0)]), t);
        assert_eq!(b.rejects(BookId(3)), 1);
        assert_eq!(ev.len(), 3);
        assert!(ev.iter().all(|e| e.text.contains("older")), "{ev:?}");
    }

    #[test]
    fn far_future_as_of_locks_out_genuine_snapshots_loudly() {
        let mut b = lb();
        let t = Instant::now();
        b.on_snapshot(
            Some(BookId(3)),
            &snap(3, u64::MAX, vec![line(AAPL, 10, 0, 0)]),
            t,
        );
        // The genuine EXO's (older) snapshots are now rejected: book invalid
        // and its underlyings WARN-forced, and it stays that way.
        let ev = b.on_snapshot(Some(BookId(3)), &snap(3, 5, vec![line(AAPL, 10, 0, 0)]), t);
        assert_eq!(ev.len(), 3);
        assert!(ev.iter().all(|e| e.level == Level::Warn));
        assert!(
            b.on_snapshot(Some(BookId(3)), &snap(3, 6, vec![line(AAPL, 10, 0, 0)]), t)
                .is_empty()
        );
        assert_eq!(b.rejects(BookId(3)), 2);
        assert!(b.tick(t).is_empty()); // still WARN, no flap
    }

    #[test]
    fn l_stale_warns_after_max_age_and_fresh_snapshot_clears() {
        let mut b = lb();
        let t0 = Instant::now();
        b.on_snapshot(Some(BookId(3)), &snap(3, 1, vec![line(AAPL, 10, 0, 0)]), t0);
        assert!(
            b.tick(t0 + MAX_AGE).is_empty(),
            "exactly max_age is not stale"
        );
        let ev = b.tick(t0 + MAX_AGE + Duration::from_secs(1));
        assert_eq!(ev.len(), 3);
        assert!(
            ev.iter()
                .all(|e| e.level == Level::Warn && e.text.contains("stale")),
            "{ev:?}"
        );
        assert!(b.tick(t0 + Duration::from_secs(60)).is_empty()); // held, no repeat
        let ev = b.on_snapshot(
            Some(BookId(3)),
            &snap(3, 2, vec![line(AAPL, 10, 0, 0)]),
            t0 + Duration::from_secs(61),
        );
        assert_eq!(ev.len(), 3);
        assert!(ev.iter().all(|e| e.level == Level::Ok));
    }

    #[test]
    fn l_never_a_book_that_never_published_raises_nothing() {
        let mut b = lb();
        let t0 = Instant::now();
        assert!(b.tick(t0).is_empty());
        assert!(b.tick(t0 + Duration::from_secs(3600)).is_empty());
        // A book whose only snapshot was rejected has no lines to hold.
        let mut bad = snap(3, 1, vec![]);
        bad.meta_complete = false;
        assert!(b.on_snapshot(Some(BookId(3)), &bad, t0).is_empty());
        assert!(b.tick(t0 + Duration::from_secs(3600)).is_empty());
    }

    #[test]
    fn l_abs_i64_min_and_overflowing_sums_do_not_panic() {
        let mut b = lb();
        let t = Instant::now();
        // i64::MIN alone: fits i64, |v| >= hard -> HARD, no abs() panic.
        let ev = b.on_snapshot(
            Some(BookId(3)),
            &snap(3, 1, vec![line(AAPL, i64::MIN, 0, 0)]),
            t,
        );
        let e = vega_events(&ev).into_iter().next().unwrap().clone();
        assert_eq!((e.level, e.value), (Level::Hard, i64::MIN));
        // Two lines at i64::MAX: sum 2^64 does not fit i64.
        let mut b = lb();
        let two_max = vec![line(AAPL, i64::MAX, 0, 0), line(AAPL, i64::MAX, 0, 0)];
        let ev = b.on_snapshot(Some(BookId(3)), &snap(3, 1, two_max), t);
        let e = vega_events(&ev).into_iter().next().unwrap().clone();
        assert_eq!((e.level, e.value), (Level::Hard, i64::MAX));
        assert!(e.text.contains("overflow"), "{}", e.text);
        // Negative overflow saturates by sign, across books.
        let mut b = lb();
        let two_min = vec![line(AAPL, i64::MIN, 0, 0), line(AAPL, i64::MIN, 0, 0)];
        let ev = b.on_snapshot(Some(BookId(3)), &snap(3, 1, two_min), t);
        let e = vega_events(&ev).into_iter().next().unwrap().clone();
        assert_eq!((e.level, e.value), (Level::Hard, i64::MIN));
        assert!(e.text.contains("overflow"), "{}", e.text);
    }

    #[test]
    fn l_cap_line_and_flagged_caps() {
        let mut b = lb();
        let t = Instant::now();
        let ok_lines: Vec<ValLine> = (0..MAX_LINES).map(|_| line(AAPL, 0, 0, 0)).collect();
        b.on_snapshot(Some(BookId(3)), &snap(3, 1, ok_lines), t);
        assert_eq!(b.rejects(BookId(3)), 0, "1,024 lines is accepted");
        let too_many: Vec<ValLine> = (0..=MAX_LINES).map(|_| line(AAPL, 0, 0, 0)).collect();
        b.on_snapshot(Some(BookId(3)), &snap(3, 2, too_many), t);
        assert_eq!(b.rejects(BookId(3)), 1);
        let mut flagged = line(AAPL, 0, 0, 0);
        flagged.n_flagged = MAX_FLAGGED_PER_LINE;
        b.on_snapshot(Some(BookId(3)), &snap(3, 3, vec![flagged]), t);
        assert_eq!(b.rejects(BookId(3)), 1, "8 flagged is accepted");
        flagged.n_flagged = MAX_FLAGGED_PER_LINE + 1;
        b.on_snapshot(Some(BookId(3)), &snap(3, 4, vec![flagged]), t);
        assert_eq!(b.rejects(BookId(3)), 2);
    }

    #[test]
    fn unknown_or_missing_underlying_is_rejected() {
        let mut b = lb();
        let t = Instant::now();
        b.on_snapshot(
            Some(BookId(3)),
            &snap(3, 1, vec![line(InstrumentId(9999), 0, 0, 0)]),
            t,
        );
        assert_eq!(b.rejects(BookId(3)), 1);
        let mut none = line(AAPL, 0, 0, 0);
        none.underlying = None;
        b.on_snapshot(Some(BookId(3)), &snap(3, 2, vec![none]), t);
        assert_eq!(b.rejects(BookId(3)), 2);
    }

    #[test]
    fn text_is_capped_on_a_utf8_boundary() {
        let s = "é".repeat(200); // 400 bytes
        let t = truncated(&s);
        assert!(t.len() <= MAX_TEXT_BYTES);
        assert!(s.starts_with(t));
    }

    #[test]
    fn rules_3_violations_after_accept_hold_last_lines_at_warn() {
        // Out-of-universe underlying, line cap and flagged cap each make the
        // book invalid; the last accepted lines stay in the aggregate (value
        // kept) and are WARN-floored; the next valid snapshot clears.
        let t = Instant::now();
        let mut flagged = line(AAPL, 0, 0, 0);
        flagged.n_flagged = MAX_FLAGGED_PER_LINE + 1;
        let bad: [Vec<ValLine>; 3] = [
            vec![line(InstrumentId(9999), 0, 0, 0)],
            (0..=MAX_LINES).map(|_| line(AAPL, 0, 0, 0)).collect(),
            vec![flagged],
        ];
        for lines in bad {
            let mut b = lb();
            b.on_snapshot(Some(BookId(3)), &snap(3, 1, vec![line(AAPL, 10, 0, 0)]), t);
            let ev = b.on_snapshot(Some(BookId(3)), &snap(3, 2, lines), t);
            assert_eq!(ev.len(), 3, "{ev:?}");
            assert!(ev.iter().all(|e| e.level == Level::Warn), "{ev:?}");
            assert_eq!(vega_events(&ev).first().unwrap().value, 10);
            let ev = b.on_snapshot(Some(BookId(3)), &snap(3, 3, vec![line(AAPL, 10, 0, 0)]), t);
            assert!(ev.iter().all(|e| e.level == Level::Ok), "{ev:?}");
        }
    }

    #[test]
    fn stale_then_rejected_then_accepted_clears_both() {
        let mut b = lb();
        let t0 = Instant::now();
        b.on_snapshot(
            Some(BookId(3)),
            &snap(3, 10, vec![line(AAPL, 10, 0, 0)]),
            t0,
        );
        let late = t0 + MAX_AGE + Duration::from_secs(1);
        assert_eq!(b.tick(late).len(), 3); // stale -> WARN
        // A rejected (replayed) snapshot does not refresh the clock and keeps WARN.
        assert!(
            b.on_snapshot(Some(BookId(3)), &snap(3, 1, vec![]), late)
                .is_empty()
        );
        let ev = b.on_snapshot(
            Some(BookId(3)),
            &snap(3, 11, vec![line(AAPL, 10, 0, 0)]),
            late,
        );
        assert_eq!(ev.len(), 3);
        assert!(ev.iter().all(|e| e.level == Level::Ok), "{ev:?}");
        assert!(b.tick(late + MAX_AGE).is_empty(), "fresh again, not stale");
    }

    #[test]
    fn overflow_across_books_is_hard_without_panic() {
        let mut b = lb();
        let t = Instant::now();
        // MIN + MAX = -1 nets to OK (level changes only: Hard then Ok) ...
        b.on_snapshot(
            Some(BookId(3)),
            &snap(3, 1, vec![line(AAPL, i64::MIN, 0, 0)]),
            t,
        );
        let ev = b.on_snapshot(
            Some(BookId(4)),
            &snap(4, 1, vec![line(AAPL, i64::MAX, 0, 0)]),
            t,
        );
        assert_eq!(vega_events(&ev).first().unwrap().level, Level::Ok);
        // ... then MIN + MIN is below i64::MIN: HARD, saturated, "overflow".
        let ev = b.on_snapshot(
            Some(BookId(4)),
            &snap(4, 2, vec![line(AAPL, i64::MIN, 0, 0)]),
            t,
        );
        let e = vega_events(&ev).into_iter().next().unwrap().clone();
        assert_eq!((e.level, e.value), (Level::Hard, i64::MIN));
        assert!(e.text.contains("overflow"), "{}", e.text);
    }

    #[test]
    fn ascii_text_is_cut_to_exactly_256_bytes() {
        assert_eq!(truncated(&"a".repeat(300)).len(), MAX_TEXT_BYTES);
        assert_eq!(truncated(&"a".repeat(256)).len(), 256);
    }

    #[test]
    fn fmt_scaled_is_integer_only() {
        assert_eq!(fmt_scaled(62_000_000_000_000, 1_000_000_000), "62000.00");
        assert_eq!(fmt_scaled(-1_500_000_000, 1_000_000_000), "-1.50");
        assert_eq!(
            fmt_scaled(i128::from(i64::MIN), 100),
            "-92233720368547758.08"
        );
    }

    // ---- firm_greeks (RiskSnapshot rows) ----

    fn full(u: InstrumentId, v: i64, g: i64, r: i64, d: i64, th: i64, dv: i64) -> ValLine {
        ValLine {
            delta_qty_e2: d,
            theta_e9: th,
            div_sens_e9: dv,
            ..line(u, v, g, r)
        }
    }

    fn last_levels(events: &[AlertEvent], acc: &mut BTreeMap<(InstrumentId, Greek), Level>) {
        for e in events {
            acc.insert((e.underlying, e.greek), e.level);
        }
    }

    #[test]
    fn firm_greeks_sums_two_books_for_all_six_greeks_and_matches_the_alert() {
        let mut b = lb();
        let t = Instant::now();
        let mut ev = b.on_snapshot(
            Some(BookId(3)),
            &snap(3, 1, vec![full(AAPL, 30, 7, 3, 1_000, -5, 11)]),
            t,
        );
        ev.extend(b.on_snapshot(
            Some(BookId(4)),
            &snap(4, 1, vec![full(AAPL, 40, 8, 4, -300, -6, 12)]),
            t,
        ));
        let rows = b.firm_greeks(t);
        assert_eq!(rows.len(), 1);
        let r = rows.get(&AAPL).unwrap();
        assert_eq!(r.underlying, AAPL);
        assert_eq!(r.exo_delta_qty_e2, 700);
        assert_eq!((r.vega_e9, r.gamma_qty_e2, r.rho_e9), (70, 15, 7));
        assert_eq!((r.theta_e9, r.div_sens_e9), (-11, 23));
        // Same key, same number as the alert (70 >= warn 50 -> Warn).
        let alert = ev.iter().rfind(|e| e.greek == Greek::Vega).unwrap();
        assert_eq!((alert.value, alert.level), (70, Level::Warn));
        assert_eq!(i128::from(alert.value), r.vega_e9);
        assert_eq!(
            (r.vega, r.gamma, r.rho),
            (Level::Warn, Level::Ok, Level::Ok)
        );
        assert!(!r.flagged && !r.suspect);
    }

    #[test]
    fn firm_greeks_flagged_follows_any_flagged_line() {
        let mut b = lb();
        let t = Instant::now();
        let mut flagged = line(AAPL, 0, 0, 0);
        flagged.n_flagged = 2;
        b.on_snapshot(Some(BookId(3)), &snap(3, 1, vec![flagged]), t);
        // A later unflagged line on the same underlying must not clear it.
        b.on_snapshot(
            Some(BookId(4)),
            &snap(4, 1, vec![line(AAPL, 0, 0, 0), line(MSFT, 0, 0, 0)]),
            t,
        );
        let rows = b.firm_greeks(t);
        assert!(rows.get(&AAPL).unwrap().flagged);
        assert!(!rows.get(&MSFT).unwrap().flagged);
    }

    #[test]
    fn row_level_equals_last_alert_level_and_boundaries_are_inclusive() {
        let mut b = lb();
        let t = Instant::now();
        let mut last = BTreeMap::new();
        // (vega, expected row level): warn=50, hard=100, symmetric negatives.
        let steps = [
            (49, Level::Ok),
            (50, Level::Warn),
            (99, Level::Warn),
            (100, Level::Hard),
            (99, Level::Warn),
            (49, Level::Ok),
            (-49, Level::Ok),
            (-50, Level::Warn),
            (-99, Level::Warn),
            (-100, Level::Hard),
            (0, Level::Ok),
        ];
        for (i, (vega, want)) in steps.into_iter().enumerate() {
            let ev = b.on_snapshot(
                Some(BookId(3)),
                &snap(3, 1 + i as u64, vec![line(AAPL, vega, 0, 0)]),
                t,
            );
            last_levels(&ev, &mut last);
            let row = b.firm_greeks(t).remove(&AAPL).unwrap();
            assert_eq!(row.vega, want, "vega {vega}");
            assert_eq!(row.vega_e9, i128::from(vega));
            for (greek, lvl) in [
                (Greek::Vega, row.vega),
                (Greek::Gamma, row.gamma),
                (Greek::Rho, row.rho),
            ] {
                assert_eq!(
                    lvl,
                    last.get(&(AAPL, greek)).copied().unwrap_or(Level::Ok),
                    "row vs last alert for {greek:?} at vega {vega}"
                );
            }
        }
    }

    #[test]
    fn warn_hard_cleared_ends_ok_on_the_row() {
        let mut b = lb();
        let t = Instant::now();
        let mut seen = Vec::new();
        for (i, vega) in [60, 120, 0].into_iter().enumerate() {
            b.on_snapshot(
                Some(BookId(3)),
                &snap(3, 1 + i as u64, vec![line(AAPL, vega, 0, 0)]),
                t,
            );
            seen.push(b.firm_greeks(t).remove(&AAPL).unwrap().vega);
        }
        assert_eq!(seen, vec![Level::Warn, Level::Hard, Level::Ok]);
    }

    #[test]
    fn stale_book_is_suspect_and_forces_warn_then_fresh_clears() {
        let mut b = lb();
        let t0 = Instant::now();
        b.on_snapshot(Some(BookId(3)), &snap(3, 1, vec![line(AAPL, 10, 0, 0)]), t0);
        let at_max = b.firm_greeks(t0 + MAX_AGE);
        assert!(!at_max.get(&AAPL).unwrap().suspect, "exactly max_age");
        let late = t0 + MAX_AGE + Duration::from_secs(1);
        // A pure read: suspect shows at once, and the alerts are still owed.
        assert!(b.firm_greeks(late).get(&AAPL).unwrap().suspect);
        assert_eq!(b.tick(late).len(), 3, "firm_greeks did not consume events");
        let r = b.firm_greeks(late).remove(&AAPL).unwrap();
        assert!(r.suspect);
        assert_eq!(
            (r.vega, r.gamma, r.rho),
            (Level::Warn, Level::Warn, Level::Warn)
        );
        assert_eq!(r.vega_e9, 10, "stale values are kept");
        b.on_snapshot(
            Some(BookId(3)),
            &snap(3, 2, vec![line(AAPL, 10, 0, 0)]),
            late,
        );
        let r = b.firm_greeks(late).remove(&AAPL).unwrap();
        assert!(!r.suspect);
        assert_eq!((r.vega, r.gamma, r.rho), (Level::Ok, Level::Ok, Level::Ok));
    }

    #[test]
    fn invalid_book_is_suspect_and_forces_warn() {
        let mut b = lb();
        let t = Instant::now();
        b.on_snapshot(Some(BookId(3)), &snap(3, 5, vec![line(AAPL, 10, 0, 0)]), t);
        // Strictly older as_of_ns: rejected, book invalid.
        b.on_snapshot(Some(BookId(3)), &snap(3, 4, vec![line(AAPL, 0, 0, 0)]), t);
        let r = b.firm_greeks(t).remove(&AAPL).unwrap();
        assert!(r.suspect);
        assert_eq!(
            (r.vega, r.gamma, r.rho),
            (Level::Warn, Level::Warn, Level::Warn)
        );
        assert_eq!(r.vega_e9, 10, "last accepted values stay in the row");
        // A second, healthy book on another underlying is not suspect.
        b.on_snapshot(Some(BookId(4)), &snap(4, 1, vec![line(MSFT, 1, 0, 0)]), t);
        assert!(!b.firm_greeks(t).get(&MSFT).unwrap().suspect);
    }

    #[test]
    fn firm_greeks_sums_overflow_as_i128_and_levels_saturate_hard() {
        let t = Instant::now();
        for sign in [1_i64, -1] {
            let v = if sign > 0 { i64::MAX } else { i64::MIN };
            let mut b = lb();
            b.on_snapshot(
                Some(BookId(3)),
                &snap(3, 1, vec![line(AAPL, v, 0, 0), line(AAPL, v, 0, 0)]),
                t,
            );
            let r = b.firm_greeks(t).remove(&AAPL).unwrap();
            assert_eq!(r.vega_e9, 2 * i128::from(v), "no i64 wrap in the sum");
            assert_eq!(r.vega, Level::Hard);
            assert_eq!(saturate_i64(r.vega_e9), v);
        }
    }

    #[test]
    fn firm_greeks_rows_are_underlyings_with_lines_and_levels_come_from_config() {
        let t = Instant::now();
        let mut b = lb();
        assert!(b.firm_greeks(t).is_empty());
        b.on_snapshot(Some(BookId(3)), &snap(3, 1, vec![line(AAPL, 120, 0, 0)]), t);
        // Replacement drops AAPL (alert clears) and adds MSFT.
        b.on_snapshot(Some(BookId(3)), &snap(3, 2, vec![line(MSFT, 1, 0, 0)]), t);
        let rows = b.firm_greeks(t);
        assert_eq!(rows.keys().copied().collect::<Vec<_>>(), vec![MSFT]);
        // The same exposure levels differently under different configured caps.
        let wide = TierOneLimits {
            vega_warn_e9: 1_000,
            vega_hard_e9: 2_000,
            ..limits()
        };
        let mut w = LimitBook::new(wide, MAX_AGE, &[BookId(3)], &[AAPL]);
        w.on_snapshot(Some(BookId(3)), &snap(3, 1, vec![line(AAPL, 120, 0, 0)]), t);
        assert_eq!(w.firm_greeks(t).get(&AAPL).unwrap().vega, Level::Ok);
        assert_eq!(w.limits().vega_hard_e9, 2_000);
        let mut n = lb();
        n.on_snapshot(Some(BookId(3)), &snap(3, 1, vec![line(AAPL, 120, 0, 0)]), t);
        assert_eq!(n.firm_greeks(t).get(&AAPL).unwrap().vega, Level::Hard);
    }
}
