//! Parses `delta-one/d1.toml`'s `[tracker]` (ADR-010 §5) and `[limits]`
//! (ADR-008 Tier-1 caps, ADR-014 §3) sections.
//!
//! Distinct from P1.M3, which deliberately moved its `[netting]` section OUT
//! to refdata (`protocol/refdata/universe.json`): that was a *market
//! convention*, defined once and consumed by every component that needs it.
//! Sampling interval and window size are *operator knobs* -- they tune how
//! THIS process reports analytics, not a fact the rest of the system must
//! agree on -- so `d1.toml` config is the correct home here, not refdata.
//!
//! `--config <path>` (`main.rs`, default `d1.toml`) selects the file, so a
//! scenario run can use `d1.scenario.toml` (1 s tracker sampling).

use std::path::Path;

use anyhow::{Context, Result};
use serde::Deserialize;

/// Parsed `[tracker]` section: sampling/windowing/publish-cadence knobs for
/// ex-post tracker analytics (ADR-010).
#[derive(Debug, Clone, Copy, Deserialize)]
pub struct TrackerConfig {
    /// Seconds per sample; `d1.toml`'s own comment: a demo-time "day".
    /// `u32`, matching the wire `TrackerAnalytics.sampling_interval_s`
    /// field's type exactly -- no narrowing cast needed between config and
    /// wire.
    pub sampling_interval_s: u32,
    /// Rolling window size, in observations, for TE/TD/cash-drag
    /// (`d1_analytics::TrackerWindow::new`'s capacity).
    pub te_window_obs: usize,
    /// Seconds between `TrackerAnalytics` publishes to NATS.
    ///
    /// ponytail: parsed but not yet wired to a separate publish cadence --
    /// this slice (P1.M5 Slice 2) publishes a `TrackerAnalytics` record on
    /// every sample, i.e. it behaves as if `publish_interval_s ==
    /// sampling_interval_s`. `TrackerAnalytics` (the proto) has no
    /// publish-cadence field of its own to decouple this from, so
    /// downsampling publishes independently of sampling is future work if
    /// the demo ever needs a slower UI cadence than the analytics window.
    pub publish_interval_s: u32,
}

/// Tier-1 per-underlying caps from `[limits.defaults]` (ADR-008). Gamma is in
/// `qty_e2` (dDelta per +1% spot); vega and rho are ccy x 1e9.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LimitDefaults {
    /// Vega warn, ccy x 1e9 per vol point.
    pub vega_warn_e9: i64,
    /// Vega hard.
    pub vega_hard_e9: i64,
    /// Gamma warn, qty_e2 per +1% spot.
    pub gamma_warn_qty_e2: i64,
    /// Gamma hard.
    pub gamma_hard_qty_e2: i64,
    /// Rho warn, ccy x 1e9 per bp.
    pub rho_warn_e9: i64,
    /// Rho hard.
    pub rho_hard_e9: i64,
}

/// Parsed `[limits]` section. Nothing but `defaults` and `valuation_max_age_s`
/// is allowed in the table (ADR-014 §3).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LimitsConfig {
    /// `[limits.defaults]`: applies to every underlying.
    pub defaults: LimitDefaults,
    /// Seconds without an accepted `ValuationSnapshot` before a book is stale
    /// and its underlyings are held at least WARN (three of EXO's 5 s
    /// republish intervals by default).
    pub valuation_max_age_s: u32,
}

/// Everything this process reads from `d1.toml`.
#[derive(Debug, Clone, Copy)]
pub struct D1Config {
    /// `[tracker]`.
    pub tracker: TrackerConfig,
    /// `[limits]`.
    pub limits: LimitsConfig,
}

/// Shape of the whole `d1.toml` file this module cares about -- the
/// `[tracker]` and `[limits]` tables; every other section (`[bus]`) belongs to
/// other components/slices and is intentionally not modeled here.
#[derive(Debug, Deserialize)]
struct RootConfig {
    tracker: TrackerConfig,
    limits: LimitsConfig,
}

/// Load and parse the `[tracker]` and `[limits]` sections out of the
/// `d1.toml` at `path`. A missing file or a missing section is a hard startup
/// error -- the same fail-loud posture `main.rs` already applies to
/// `cross_px_policy` (never a silent default): a process that started with no
/// sampling config would either publish nothing or fall back to a made-up
/// cadence, and one with no limits would silently never alert.
pub fn load(path: &Path) -> Result<D1Config> {
    let raw =
        std::fs::read_to_string(path).with_context(|| format!("reading d1 config at {path:?}"))?;
    parse(&raw).with_context(|| format!("parsing d1 config at {path:?}"))
}

/// `load`'s parse-and-validate half, on the file's text.
fn parse(raw: &str) -> Result<D1Config> {
    let parsed: RootConfig = toml::from_str(raw).context("[tracker]/[limits] sections")?;
    let cfg = parsed.tracker;
    // Fail loud on a degenerate `[tracker]` config, same posture as the
    // missing-file/missing-section cases above: `te_window_obs < 2` makes
    // `d1_analytics::analytics` unconditionally return `None` (its own `n <
    // 2` floor) and `sampling_interval_s == 0` silently disables the
    // sampling driver (`crates/d1/src/lib.rs::run_core`'s `sampling_interval_ns
    // > 0` guard) -- both would ship a process that looks configured for
    // tracker analytics but publishes none of it, with no error anywhere.
    anyhow::ensure!(
        cfg.te_window_obs >= 2,
        "[tracker].te_window_obs must be >= 2 (analytics needs at least 2 observations), got {}",
        cfg.te_window_obs
    );
    // Upper bound (security review, LOW): `TrackerWindow::new` sizes a
    // `VecDeque::with_capacity(te_window_obs)` straight from this config, so
    // a pathological value panics the startup allocation instead of failing
    // gracefully -- same fail-loud-at-config-parse posture as the lower
    // bound above, not a hot-path check.
    anyhow::ensure!(
        cfg.te_window_obs <= 10_000,
        "[tracker].te_window_obs must be <= 10_000, got {}",
        cfg.te_window_obs
    );
    anyhow::ensure!(
        cfg.sampling_interval_s > 0,
        "[tracker].sampling_interval_s must be > 0 (0 silently disables sampling)"
    );
    validate_limits(&parsed.limits)?;
    Ok(D1Config {
        tracker: cfg,
        limits: parsed.limits,
    })
}

/// Largest accepted `[limits] valuation_max_age_s` (one hour).
const MAX_VALUATION_MAX_AGE_S: u32 = 3600;

/// ADR-014 §3: every cap `> 0`, and `warn < hard` per Greek (strict, since
/// `warn == hard` would make WARN unreachable); `valuation_max_age_s > 0`
/// (0 would make every book permanently stale).
fn validate_limits(limits: &LimitsConfig) -> Result<()> {
    let d = &limits.defaults;
    for (greek, warn, hard) in [
        ("vega_e9", d.vega_warn_e9, d.vega_hard_e9),
        ("gamma_qty_e2", d.gamma_warn_qty_e2, d.gamma_hard_qty_e2),
        ("rho_e9", d.rho_warn_e9, d.rho_hard_e9),
    ] {
        anyhow::ensure!(
            warn > 0 && hard > 0,
            "[limits.defaults].{greek}: warn ({warn}) and hard ({hard}) must both be > 0"
        );
        anyhow::ensure!(
            warn < hard,
            "[limits.defaults].{greek}: warn ({warn}) must be < hard ({hard})"
        );
    }
    // Upper bound: u32::MAX seconds would disable staleness (a silent feed
    // would never hold its underlyings at WARN).
    anyhow::ensure!(
        (1..=MAX_VALUATION_MAX_AGE_S).contains(&limits.valuation_max_age_s),
        "[limits].valuation_max_age_s must be in 1..={MAX_VALUATION_MAX_AGE_S}, got {}",
        limits.valuation_max_age_s
    );
    Ok(())
}

#[cfg(test)]
#[allow(clippy::unwrap_used)] // tests: unwrap_used/expect_used are hot-path-only bans (delta-one/CLAUDE.md)
mod tests {
    use super::*;

    /// `crates/d1` -> `crates` -> `delta-one`, where the real `d1.toml` lives
    /// -- same depth-from-manifest-dir shape as `d1-refdata`'s own
    /// `universe_path()` test helper.
    fn real_d1_toml_path() -> std::path::PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../d1.toml")
    }

    #[test]
    fn parses_real_d1_toml() {
        let cfg = load(&real_d1_toml_path()).unwrap();
        assert_eq!(cfg.tracker.sampling_interval_s, 60);
        assert_eq!(cfg.tracker.te_window_obs, 250);
        assert_eq!(cfg.tracker.publish_interval_s, 60);
        let d = cfg.limits.defaults;
        assert_eq!(d.vega_warn_e9, 50_000_000_000_000);
        assert_eq!(d.vega_hard_e9, 100_000_000_000_000);
        assert_eq!(d.gamma_warn_qty_e2, 2_000_000);
        assert_eq!(d.gamma_hard_qty_e2, 5_000_000);
        assert_eq!(d.rho_warn_e9, 10_000_000_000_000);
        assert_eq!(d.rho_hard_e9, 25_000_000_000_000);
        assert_eq!(cfg.limits.valuation_max_age_s, 15);
    }

    #[test]
    fn parses_real_scenario_toml() {
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../d1.scenario.toml");
        let cfg = load(&path).unwrap();
        assert_eq!(cfg.tracker.sampling_interval_s, 1);
        assert_eq!(cfg.tracker.publish_interval_s, 1);
        // The whole [limits] section is identical to the production file, and
        // so is every tracker knob except the two intended cadence overrides,
        // so the copy cannot drift.
        let prod = load(&real_d1_toml_path()).unwrap();
        assert_eq!(cfg.limits, prod.limits);
        assert_eq!(cfg.tracker.te_window_obs, prod.tracker.te_window_obs);
    }

    #[test]
    fn missing_tracker_section_is_an_error() {
        let dir = std::env::temp_dir().join("d1-config-test-missing-tracker");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("d1.toml");
        std::fs::write(&path, "[bus]\nnats_url = \"nats://localhost:4222\"\n").unwrap();
        assert!(load(&path).is_err());
    }

    #[test]
    fn missing_file_is_an_error() {
        assert!(load(Path::new("/nonexistent/path/d1.toml")).is_err());
    }

    #[test]
    fn te_window_obs_below_two_is_an_error() {
        let dir = std::env::temp_dir().join("d1-config-test-window-too-small");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("d1.toml");
        std::fs::write(
            &path,
            format!(
                "{}{LIMITS}",
                "[tracker]\nsampling_interval_s = 60\nte_window_obs = 1\npublish_interval_s = 60\n"
            ),
        )
        .unwrap();
        assert!(format!("{:#}", load(&path).unwrap_err()).contains("te_window_obs must be >= 2"));
    }

    #[test]
    fn te_window_obs_above_upper_bound_is_an_error() {
        let dir = std::env::temp_dir().join("d1-config-test-window-too-large");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("d1.toml");
        std::fs::write(
            &path,
            format!("{}{LIMITS}", "[tracker]\nsampling_interval_s = 60\nte_window_obs = 10001\npublish_interval_s = 60\n"),
        )
        .unwrap();
        assert!(
            format!("{:#}", load(&path).unwrap_err()).contains("te_window_obs must be <= 10_000")
        );
    }

    #[test]
    fn zero_sampling_interval_is_an_error() {
        let dir = std::env::temp_dir().join("d1-config-test-zero-sampling");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("d1.toml");
        std::fs::write(
            &path,
            format!(
                "{}{LIMITS}",
                "[tracker]\nsampling_interval_s = 0\nte_window_obs = 250\npublish_interval_s = 60\n"
            ),
        )
        .unwrap();
        assert!(
            format!("{:#}", load(&path).unwrap_err()).contains("sampling_interval_s must be > 0")
        );
    }

    const TRACKER: &str =
        "[tracker]\nsampling_interval_s = 60\nte_window_obs = 250\npublish_interval_s = 60\n";
    const LIMITS: &str = "[limits]\nvaluation_max_age_s = 15\n[limits.defaults]\n\
         vega_warn_e9 = 50\nvega_hard_e9 = 100\ngamma_warn_qty_e2 = 20\ngamma_hard_qty_e2 = 50\n\
         rho_warn_e9 = 10\nrho_hard_e9 = 25\n";

    fn parse_err(raw: &str) -> String {
        format!("{:#}", parse(raw).unwrap_err())
    }

    #[test]
    fn minimal_valid_config_parses() {
        parse(&format!("{TRACKER}{LIMITS}")).unwrap();
    }

    #[test]
    fn missing_limits_section_is_an_error() {
        assert!(parse_err(TRACKER).contains("missing field `limits`"));
    }

    #[test]
    fn missing_max_age_is_an_error() {
        let limits = LIMITS.replace("valuation_max_age_s = 15\n", "");
        assert!(
            parse_err(&format!("{TRACKER}{limits}"))
                .contains("missing field `valuation_max_age_s`")
        );
    }

    #[test]
    fn zero_and_negative_limits_are_errors() {
        for (old, new) in [
            ("vega_warn_e9 = 50", "vega_warn_e9 = 0"),
            ("vega_warn_e9 = 50", "vega_warn_e9 = -5"),
            ("rho_hard_e9 = 25", "rho_hard_e9 = 0"),
            ("gamma_hard_qty_e2 = 50", "gamma_hard_qty_e2 = -1"),
        ] {
            let limits = LIMITS.replace(old, new);
            assert!(parse_err(&format!("{TRACKER}{limits}")).contains("must both be > 0"));
        }
    }

    #[test]
    fn warn_equal_or_above_hard_is_an_error() {
        for (warn, hard) in [(100, 100), (150, 100)] {
            let limits = LIMITS
                .replace("vega_warn_e9 = 50", &format!("vega_warn_e9 = {warn}"))
                .replace("vega_hard_e9 = 100", &format!("vega_hard_e9 = {hard}"));
            assert!(parse_err(&format!("{TRACKER}{limits}")).contains("must be < hard"));
        }
    }

    #[test]
    fn zero_max_age_is_an_error() {
        let limits = LIMITS.replace("valuation_max_age_s = 15", "valuation_max_age_s = 0");
        assert!(parse_err(&format!("{TRACKER}{limits}")).contains("must be in 1..=3600"));
    }

    #[test]
    fn max_age_above_one_hour_is_an_error_and_one_hour_is_accepted() {
        let at = LIMITS.replace("valuation_max_age_s = 15", "valuation_max_age_s = 3600");
        parse(&format!("{TRACKER}{at}")).unwrap();
        let over = LIMITS.replace("valuation_max_age_s = 15", "valuation_max_age_s = 3601");
        assert!(parse_err(&format!("{TRACKER}{over}")).contains("must be in 1..=3600"));
        let max = LIMITS.replace(
            "valuation_max_age_s = 15",
            "valuation_max_age_s = 4294967295",
        );
        assert!(parse_err(&format!("{TRACKER}{max}")).contains("must be in 1..=3600"));
    }

    #[test]
    fn unknown_keys_in_limits_are_errors() {
        // Directly under [limits]:
        let top = LIMITS.replace(
            "valuation_max_age_s = 15\n",
            "valuation_max_age_s = 15\nvega_warn_e9 = 1\n",
        );
        assert!(parse_err(&format!("{TRACKER}{top}")).contains("unknown field `vega_warn_e9`"));
        // Inside [limits.defaults]:
        let nested = format!("{LIMITS}delta_warn_e9 = 1\n");
        assert!(parse_err(&format!("{TRACKER}{nested}")).contains("unknown field `delta_warn_e9`"));
    }
}
