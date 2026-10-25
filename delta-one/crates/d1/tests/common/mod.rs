//! Shared by the five `--ignored` infra tests (nats_round_trip, crosses_round_trip,
//! loop_round_trip, golden_posttrade, storyline).
#![allow(clippy::panic)] // integration test helper, not hot-path code

/// Called when a service the test needs is unreachable. By default the test
/// prints `what` and the caller returns (a skip counts as a pass, so an
/// accidental `--ignored` run with no `just up` is harmless). With
/// `INFRA_REQUIRED=1` (CI, scripts/demo.sh) it fails instead, so a broken or
/// missing stack can never read as green.
pub fn skip_unless_required(test: &str, what: &str) {
    assert!(
        std::env::var("INFRA_REQUIRED").as_deref() != Ok("1"),
        "{test}: {what}, and INFRA_REQUIRED=1 forbids skipping (`just up` first)"
    );
    println!("{test}: {what}, skipping (`just up` first)");
}
