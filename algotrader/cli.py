from __future__ import annotations

import argparse
from pathlib import Path

from algotrader.config import AppSettings, StrategyConfig
from algotrader.engine import TradingEngine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OptionTrader for Indian index and stock options")
    parser.add_argument("--config", required=True, help="Path to strategy JSON file")
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="Execution mode",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one evaluation cycle and exit",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    strategy_config = StrategyConfig.from_json(Path(args.config))
    settings = AppSettings.from_env()
    engine = TradingEngine(
        settings=settings,
        strategy_config=strategy_config,
        execution_mode=args.mode,
    )

    if args.once:
        engine.run_once()
        return 0

    engine.run_forever()
    return 0
