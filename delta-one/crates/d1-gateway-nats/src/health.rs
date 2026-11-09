//! `d1.health` content (ADR-014 Amendment 1 §2): the pure function that turns
//! the gateway's view of the process into `HealthStatus.ok` and `detail`.
//!
//! Std types only, so the FIX and Kafka threads can own their flags without
//! depending on this crate's gateway code. Every `detail` value is a fixed
//! token or a decimal `u64`; a payload-derived string never enters the map
//! (ADR-014 §14).

use std::collections::BTreeMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::Instant;

use d1_core::BookId;

use crate::limits::{LimitBook, ValuationState};

/// The Kafka producer thread's liveness, written by that thread and read by
/// the gateway thread for display only (no other state is synchronised through
/// these, so `Relaxed` is sufficient everywhere).
#[derive(Debug, Default)]
pub struct PostTradeHealth {
    /// Set once the schema registry accepted all schemas, i.e. the producer is
    /// about to drain.
    pub ready: AtomicBool,
    /// Set when the producer thread exited with an error.
    pub failed: AtomicBool,
    /// Local `send()` failures (queue full, message too large, ...).
    pub send_failures: AtomicU64,
}

/// What `health_detail` reads besides the NATS connection state and the
/// limit book.
#[derive(Debug, Clone)]
pub struct HealthInputs {
    /// `FixCallbacks::logged_on_handle()`.
    pub fix_logged_on: Arc<AtomicBool>,
    /// `None` when no Kafka producer was spawned.
    pub posttrade: Option<Arc<PostTradeHealth>>,
}

/// `(ok, detail)` for one `HealthStatus`.
///
/// `ok` = FIX logged on AND (post-trade disabled OR running). Limit levels
/// and valuation states are reported in `detail` but never affect `ok`.
///
/// Keys: `nats`, `fix.session`, `posttrade`, `posttrade.send_failures`
/// (absent when disabled), `limits.unattributable_rejects`, and for every
/// book in `books` `limits.book.<id>.rejects` / `limits.book.<id>.valuation`.
#[must_use]
pub fn health_detail(
    nats: async_nats::connection::State,
    inputs: &HealthInputs,
    limits: &LimitBook,
    books: &[BookId],
    now: Instant,
) -> (bool, BTreeMap<String, String>) {
    use async_nats::connection::State;

    let mut detail = BTreeMap::new();
    let nats = match nats {
        State::Connected => "connected",
        State::Pending => "pending",
        State::Disconnected => "disconnected",
    };
    detail.insert("nats".to_string(), nats.to_string());

    // Display-only flag; `Relaxed` (see `PostTradeHealth`).
    let logged_on = inputs.fix_logged_on.load(Ordering::Relaxed);
    let session = if logged_on { "logged_on" } else { "logged_out" };
    detail.insert("fix.session".to_string(), session.to_string());

    let posttrade_ok = match &inputs.posttrade {
        None => {
            detail.insert("posttrade".to_string(), "disabled".to_string());
            true
        }
        Some(pt) => {
            // `failed` wins: a producer that ran and then died is not running.
            let state = if pt.failed.load(Ordering::Relaxed) {
                "failed"
            } else if pt.ready.load(Ordering::Relaxed) {
                "running"
            } else {
                "starting"
            };
            detail.insert("posttrade".to_string(), state.to_string());
            detail.insert(
                "posttrade.send_failures".to_string(),
                pt.send_failures.load(Ordering::Relaxed).to_string(),
            );
            state == "running"
        }
    };

    detail.insert(
        "limits.unattributable_rejects".to_string(),
        limits.unattributable_rejects().to_string(),
    );
    for &book in books {
        let id = book.0;
        detail.insert(
            format!("limits.book.{id}.rejects"),
            limits.rejects(book).to_string(),
        );
        let valuation = match limits.valuation_state(book, now) {
            ValuationState::None => "none",
            ValuationState::Ok => "ok",
            ValuationState::Stale => "stale",
            ValuationState::Invalid => "invalid",
        };
        detail.insert(format!("limits.book.{id}.valuation"), valuation.to_string());
    }

    (logged_on && posttrade_ok, detail)
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;
    use std::time::Duration;

    use async_nats::connection::State;
    use d1_core::InstrumentId;

    use super::*;
    use crate::limits::{TierOneLimits, ValLine, Valuation};

    /// Map lookup that cannot panic (indexing is deny-linted in tests too); a
    /// missing key yields a sentinel that fails the surrounding assertion.
    fn at<'a>(d: &'a BTreeMap<String, String>, k: &str) -> &'a str {
        d.get(k).map_or("<missing>", String::as_str)
    }

    const AAPL: InstrumentId = InstrumentId(1001);
    const MAX_AGE: Duration = Duration::from_secs(15);

    fn books5() -> Vec<BookId> {
        (1..=5).map(BookId).collect()
    }

    fn lb(books: &[BookId]) -> LimitBook {
        LimitBook::new(
            TierOneLimits {
                vega_warn_e9: 50,
                vega_hard_e9: 100,
                gamma_warn_qty_e2: 20,
                gamma_hard_qty_e2: 50,
                rho_warn_e9: 10,
                rho_hard_e9: 25,
            },
            MAX_AGE,
            books,
            &[AAPL],
        )
    }

    fn snap(book: u32, as_of_ns: u64, meta_complete: bool) -> Valuation {
        Valuation {
            book: BookId(book),
            as_of_ns,
            meta_complete,
            lines: vec![ValLine {
                underlying: Some(AAPL),
                vega_e9: 1,
                gamma_qty_e2: 0,
                rho_e9: 0,
                delta_qty_e2: 0,
                theta_e9: 0,
                div_sens_e9: 0,
                n_flagged: 0,
            }],
        }
    }

    /// (ready, failed) of the producer's flags.
    type PtFlags = (bool, bool);

    fn inputs(logged_on: bool, pt: Option<PtFlags>) -> HealthInputs {
        HealthInputs {
            fix_logged_on: Arc::new(AtomicBool::new(logged_on)),
            posttrade: pt.map(|(ready, failed)| {
                Arc::new(PostTradeHealth {
                    ready: AtomicBool::new(ready),
                    failed: AtomicBool::new(failed),
                    send_failures: AtomicU64::new(0),
                })
            }),
        }
    }

    #[test]
    fn exact_key_set_for_five_books() {
        let books = books5();
        let (_, detail) = health_detail(
            State::Connected,
            &inputs(true, Some((true, false))),
            &lb(&books),
            &books,
            Instant::now(),
        );
        let mut want: BTreeSet<String> = [
            "nats",
            "fix.session",
            "posttrade",
            "posttrade.send_failures",
            "limits.unattributable_rejects",
        ]
        .iter()
        .map(|s| (*s).to_string())
        .collect();
        for b in 1..=5 {
            want.insert(format!("limits.book.{b}.rejects"));
            want.insert(format!("limits.book.{b}.valuation"));
        }
        assert_eq!(want.len(), 15); // 5 fixed + 2 per book x 5
        assert_eq!(detail.keys().cloned().collect::<BTreeSet<_>>(), want);
    }

    #[test]
    fn disabled_posttrade_has_no_send_failures_key() {
        let books = books5();
        let (_, detail) = health_detail(
            State::Connected,
            &inputs(true, None),
            &lb(&books),
            &books,
            Instant::now(),
        );
        assert_eq!(detail.len(), 14);
        assert_eq!(at(&detail, "posttrade"), "disabled");
        assert!(!detail.contains_key("posttrade.send_failures"));
    }

    #[test]
    fn ok_truth_table() {
        let books = books5();
        let now = Instant::now();
        // (posttrade input, token, posttrade counts as ok)
        let cases: [(Option<PtFlags>, &str, bool); 5] = [
            (None, "disabled", true),
            (Some((false, false)), "starting", false),
            (Some((true, false)), "running", true),
            (Some((false, true)), "failed", false),
            (Some((true, true)), "failed", false),
        ];
        for logged_on in [false, true] {
            for (pt, token, pt_ok) in cases {
                let (ok, detail) = health_detail(
                    State::Connected,
                    &inputs(logged_on, pt),
                    &lb(&books),
                    &books,
                    now,
                );
                assert_eq!(at(&detail, "posttrade"), token);
                assert_eq!(
                    at(&detail, "fix.session"),
                    if logged_on { "logged_on" } else { "logged_out" }
                );
                assert_eq!(ok, logged_on && pt_ok, "logged_on={logged_on} pt={pt:?}");
            }
        }
    }

    #[test]
    fn nats_state_tokens_and_ok_independence() {
        let books = books5();
        for (state, token) in [
            (State::Connected, "connected"),
            (State::Pending, "pending"),
            (State::Disconnected, "disconnected"),
        ] {
            let (ok, detail) = health_detail(
                state,
                &inputs(true, None),
                &lb(&books),
                &books,
                Instant::now(),
            );
            assert_eq!(at(&detail, "nats"), token);
            // This process publishes the status, so the state is reported, not gated on.
            assert!(ok);
        }
    }

    #[test]
    fn send_failures_is_reported() {
        let books = books5();
        let i = inputs(true, Some((true, false)));
        if let Some(pt) = &i.posttrade {
            pt.send_failures.store(7, Ordering::Relaxed);
        }
        let (ok, detail) = health_detail(State::Connected, &i, &lb(&books), &books, Instant::now());
        assert!(ok, "send failures alone do not fail health");
        assert_eq!(at(&detail, "posttrade.send_failures"), "7");
    }

    #[test]
    fn valuation_state_transitions_and_limits_never_affect_ok() {
        let books = books5();
        let mut lb = lb(&books);
        let t0 = Instant::now();
        let i = inputs(true, None);
        let state = |lb: &LimitBook, b: u32, now| {
            let (ok, d) = health_detail(State::Connected, &i, lb, &books, now);
            assert!(ok, "valuation state must never affect ok");
            at(&d, &format!("limits.book.{b}.valuation")).to_owned()
        };
        let enum_of = |lb: &LimitBook, b: u32, now| lb.valuation_state(BookId(b), now);

        // none -> ok -> stale -> invalid -> ok
        assert_eq!(state(&lb, 3, t0), "none");
        assert_eq!(enum_of(&lb, 3, t0), ValuationState::None);
        lb.on_snapshot(Some(BookId(3)), &snap(3, 10, true), t0);
        assert_eq!(state(&lb, 3, t0), "ok");
        let stale_at = t0 + MAX_AGE + Duration::from_secs(1);
        assert_eq!(state(&lb, 3, stale_at), "stale");
        lb.on_snapshot(Some(BookId(3)), &snap(3, 11, false), stale_at); // meta incomplete
        assert_eq!(state(&lb, 3, stale_at), "invalid");
        lb.on_snapshot(Some(BookId(3)), &snap(3, 12, true), stale_at);
        assert_eq!(state(&lb, 3, stale_at), "ok");
        // Other books are untouched by book 3's history.
        assert_eq!(state(&lb, 4, stale_at), "none");

        // First-ever snapshot rejected -> invalid (not none).
        lb.on_snapshot(Some(BookId(5)), &snap(5, 1, false), t0);
        assert_eq!(state(&lb, 5, t0), "invalid");
        assert_eq!(enum_of(&lb, 5, t0), ValuationState::Invalid);
    }

    #[test]
    fn reject_counters_match_limit_book() {
        let books = books5();
        let mut lb = lb(&books);
        let t0 = Instant::now();
        lb.on_snapshot(Some(BookId(2)), &snap(2, 1, false), t0);
        lb.on_snapshot(Some(BookId(2)), &snap(2, 2, false), t0);
        lb.on_snapshot(Some(BookId(4)), &snap(4, 1, false), t0);
        lb.reject_unattributable("junk");
        lb.on_snapshot(Some(BookId(9)), &snap(9, 1, true), t0); // not in universe
        let (_, detail) = health_detail(State::Connected, &inputs(true, None), &lb, &books, t0);
        assert_eq!(at(&detail, "limits.unattributable_rejects"), "2");
        assert_eq!(lb.unattributable_rejects(), 2);
        for b in &books {
            assert_eq!(
                at(&detail, &format!("limits.book.{}.rejects", b.0)),
                lb.rejects(*b).to_string()
            );
        }
        assert_eq!(at(&detail, "limits.book.2.rejects"), "2");
        assert_eq!(at(&detail, "limits.book.4.rejects"), "1");
        assert_eq!(at(&detail, "limits.book.1.rejects"), "0");
    }

    #[test]
    fn values_are_fixed_tokens_or_decimal_u64_only() {
        let books = books5();
        let mut lb = lb(&books);
        let t0 = Instant::now();
        // Feed in payload-derived strings; none may reach `detail`.
        lb.reject_unattributable("<script>alert(1)</script> Hostile Reason");
        lb.on_snapshot(Some(BookId(1)), &snap(1, 1, false), t0);
        let i = inputs(true, Some((true, false)));
        let (_, detail) = health_detail(State::Connected, &i, &lb, &books, t0);
        // `_` is in the set because the spec's tokens `logged_on`/`logged_out` carry one.
        let token =
            |v: &str| !v.is_empty() && v.chars().all(|c| matches!(c, 'a'..='z' | '0'..='9' | '_'));
        for (k, v) in &detail {
            assert!(token(v), "{k} = {v:?} is not a fixed token or decimal");
            assert!(
                k.chars()
                    .all(|c| matches!(c, 'a'..='z' | '0'..='9' | '.' | '_')),
                "key {k:?}"
            );
            if !matches!(k.as_str(), "nats" | "fix.session" | "posttrade")
                && !k.ends_with(".valuation")
            {
                assert!(v.parse::<u64>().is_ok(), "{k} = {v:?} is not a decimal u64");
            }
        }
    }
}
