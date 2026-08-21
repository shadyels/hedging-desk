//! Simulator binary: market-data generator + FIX acceptor + scenario runner.
//! Rules and scope: /sim/CLAUDE.md. Scenarios: /sim/scenarios/*.yaml.
//! Implemented in P1.M1–P1.M2 (docs/ROADMAP.md). M1: `replay` mode only.

mod acceptor;
mod emit;
mod replay;
mod scenario;

use std::path::PathBuf;

use acceptor::FillModel;
use anyhow::{Result, bail};

/// Default acceptor session config, relative to the workspace root
/// (`delta-one/`) -- matches how `justfile`'s `sim` target and this binary's
/// other relative paths (e.g. `../sim/scenarios/...`) are already resolved.
const DEFAULT_ACCEPTOR_CFG: &str = "crates/sim/acceptor.cfg";

/// Must match `crates/sim/acceptor.cfg`'s `[SESSION]` block by default;
/// overridable via `--sender-comp-id`/`--target-comp-id` for a test harness
/// that needs a unique session identity per run (quickfix's session registry
/// is process-global, keyed by these ids -- see `crates/d1/tests/fix_round_trip.rs`).
const DEFAULT_SENDER_COMP_ID: &str = "SIM";
const DEFAULT_TARGET_COMP_ID: &str = "D1";

enum Mode {
    Replay(PathBuf),
    Acceptor {
        cfg: PathBuf,
        fill_model: FillModel,
        sender_comp_id: String,
        target_comp_id: String,
    },
    EmitTicks {
        scenario: PathBuf,
        out: PathBuf,
    },
}

/// Which of the three run modes `--mode` selects. Distinct from `Mode`
/// (this file's dispatch payload, built only once every flag has been
/// parsed and each mode's required flags are known to be present) --
/// hand-rolled like the rest of this file's arg parsing, no CLI crate.
#[derive(Clone, Copy)]
enum RunMode {
    Replay,
    Acceptor,
    EmitTicks,
}

fn main() -> Result<()> {
    match parse_args()? {
        Mode::Replay(scenario_path) => replay::run(&scenario_path),
        Mode::Acceptor {
            cfg,
            fill_model,
            sender_comp_id,
            target_comp_id,
        } => acceptor::run(&cfg, fill_model, &sender_comp_id, &target_comp_id),
        Mode::EmitTicks { scenario, out } => emit::run(&scenario, &out),
    }
}

fn parse_args() -> Result<Mode> {
    let mut args = std::env::args().skip(1);
    let mut scenario: Option<PathBuf> = None;
    let mut run_mode = RunMode::Replay;
    let mut cfg: Option<PathBuf> = None;
    let mut fill_model = FillModel::ImmediateFull;
    let mut sender_comp_id = DEFAULT_SENDER_COMP_ID.to_string();
    let mut target_comp_id = DEFAULT_TARGET_COMP_ID.to_string();
    let mut out: Option<PathBuf> = None;

    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--scenario" => {
                scenario = Some(
                    args.next()
                        .map(PathBuf::from)
                        .ok_or_else(|| anyhow::anyhow!("--scenario requires a path"))?,
                );
            }
            "--mode" => {
                let value = args
                    .next()
                    .ok_or_else(|| anyhow::anyhow!("--mode requires a value"))?;
                run_mode = match value.as_str() {
                    "replay" => RunMode::Replay,
                    "acceptor" => RunMode::Acceptor,
                    "emit-ticks" => RunMode::EmitTicks,
                    other => bail!("unknown --mode '{other}' (want replay|acceptor|emit-ticks)"),
                };
            }
            "--out" => {
                out = Some(
                    args.next()
                        .map(PathBuf::from)
                        .ok_or_else(|| anyhow::anyhow!("--out requires a path"))?,
                );
            }
            "--cfg" => {
                cfg = Some(
                    args.next()
                        .map(PathBuf::from)
                        .ok_or_else(|| anyhow::anyhow!("--cfg requires a path"))?,
                );
            }
            "--fill-model" => {
                let value = args
                    .next()
                    .ok_or_else(|| anyhow::anyhow!("--fill-model requires a value"))?;
                fill_model = value.parse()?;
            }
            "--sender-comp-id" => {
                sender_comp_id = args
                    .next()
                    .ok_or_else(|| anyhow::anyhow!("--sender-comp-id requires a value"))?;
            }
            "--target-comp-id" => {
                target_comp_id = args
                    .next()
                    .ok_or_else(|| anyhow::anyhow!("--target-comp-id requires a value"))?;
            }
            other => bail!("unknown argument '{other}'"),
        }
    }

    match run_mode {
        RunMode::Acceptor => {
            let cfg = cfg.unwrap_or_else(|| PathBuf::from(DEFAULT_ACCEPTOR_CFG));
            Ok(Mode::Acceptor {
                cfg,
                fill_model,
                sender_comp_id,
                target_comp_id,
            })
        }
        RunMode::EmitTicks => {
            let scenario = scenario
                .ok_or_else(|| anyhow::anyhow!("--mode emit-ticks requires --scenario <path>"))?;
            let out =
                out.ok_or_else(|| anyhow::anyhow!("--mode emit-ticks requires --out <path>"))?;
            Ok(Mode::EmitTicks { scenario, out })
        }
        RunMode::Replay => {
            let scenario = scenario.ok_or_else(|| {
                anyhow::anyhow!(
                    "usage: sim --scenario <path/to/scenario.yaml>  (or sim --mode acceptor|emit-ticks)"
                )
            })?;
            Ok(Mode::Replay(scenario))
        }
    }
}
