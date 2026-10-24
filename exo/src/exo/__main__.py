"""python -m exo [--config PATH] -- the EXO service entrypoint (P2.M4, `bus/service.py`)."""

import argparse
import asyncio
import logging
from pathlib import Path

from exo.bus.service import FatalError, default_config_path, main
from exo.config import load_config


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="exo", description="EXO pricing and rehedging service")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="exo.toml path")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(main(load_config(args.config)))
    except FatalError as exc:
        logging.getLogger("exo").critical("fatal: %s", exc)
        raise SystemExit(3) from exc


if __name__ == "__main__":
    cli()
