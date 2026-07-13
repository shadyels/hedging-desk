//! Simulator binary: market-data generator + FIX acceptor + scenario runner.
//! Rules and scope: /sim/CLAUDE.md. Scenarios: /sim/scenarios/*.yaml.
//! Implemented in P1.M1–P1.M2 (docs/ROADMAP.md). M1: `replay` mode only.

mod refdata;
mod replay;
mod scenario;

use std::path::PathBuf;

use anyhow::{Result, bail};

fn main() -> Result<()> {
    let scenario_path = parse_args()?;
    replay::run(&scenario_path)
}

fn parse_args() -> Result<PathBuf> {
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        if arg == "--scenario" {
            return args
                .next()
                .map(PathBuf::from)
                .ok_or_else(|| anyhow::anyhow!("--scenario requires a path"));
        }
    }
    bail!("usage: sim --scenario <path/to/scenario.yaml>");
}
