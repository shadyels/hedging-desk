//! Parses `delta-one/d1.toml`'s `[tracker]` section (ADR-010 §5).
//!
//! Distinct from P1.M3, which deliberately moved its `[netting]` section OUT
//! to refdata (`protocol/refdata/universe.json`): that was a *market
//! convention*, defined once and consumed by every component that needs it.
//! Sampling interval and window size are *operator knobs* -- they tune how
//! THIS process reports analytics, not a fact the rest of the system must
//! agree on -- so `d1.toml` config is the correct home here, not refdata.
//!
//! No `--config` CLI flag (YAGNI, ponytail): `main.rs` reads this file at a
//! fixed relative path, the same convention `main.rs::DEFAULT_UNIVERSE`
//! already uses for `universe.json`.

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

/// Shape of the whole `d1.toml` file this module cares about -- only the
/// `[tracker]` table; every other section (`[bus]`, `[limits]`) belongs to
/// other components/slices and is intentionally not modeled here.
#[derive(Debug, Deserialize)]
struct RootConfig {
    tracker: TrackerConfig,
}

/// Load and parse the `[tracker]` section out of the `d1.toml` at `path`. A
/// missing file or a missing `[tracker]` section is a hard startup error --
/// the same fail-loud posture `main.rs` already applies to `cross_px_policy`
/// (never a silent default): a process that started with no sampling config
/// would either publish nothing or fall back to a made-up cadence with no
/// operator visibility into which one happened.
pub fn load(path: &Path) -> Result<TrackerConfig> {
    let raw =
        std::fs::read_to_string(path).with_context(|| format!("reading d1 config at {path:?}"))?;
    let parsed: RootConfig = toml::from_str(&raw)
        .with_context(|| format!("parsing [tracker] section of d1 config at {path:?}"))?;
    Ok(parsed.tracker)
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
        assert_eq!(cfg.sampling_interval_s, 60);
        assert_eq!(cfg.te_window_obs, 250);
        assert_eq!(cfg.publish_interval_s, 60);
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
}
