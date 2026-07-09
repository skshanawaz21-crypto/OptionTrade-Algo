from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from algotrader.cloud_state import CloudStateStore
from algotrader.config import AppSettings, StrategyConfig


DEFAULT_CONFIG = ROOT / "config" / "strategy.v1.nifty250_scanner_options.json"
DEFAULT_STATE = ROOT / "data" / "paper_state.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Initialize/migrate the OptionTrader self-hosted paper database."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Strategy config path")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="Existing paper_state.json path")
    parser.add_argument("--db", default="", help="Override DB path; defaults to OPTIONTRADER_DB_PATH/data/optiontrader.db")
    parser.add_argument(
        "--no-migrate-state",
        action="store_true",
        help="Create users/accounts/strategies but do not copy paper_state.json into the DB",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config_path = Path(args.config)
    state_path = Path(args.state)
    settings = AppSettings.from_env()
    strategy_config = StrategyConfig.from_json(config_path)
    store = CloudStateStore(Path(args.db) if args.db else None)
    store.initialize()
    context = store.ensure_default_context(
        capital=strategy_config.capital or settings.capital,
        max_daily_loss=strategy_config.risk.max_daily_loss,
        max_open_positions=strategy_config.risk.max_open_positions,
    )
    store.seed_default_strategies(
        scanner_enabled=bool((strategy_config.scanner_2m_nifty250 or {}).get("enabled", False)),
        index_scanner_enabled=bool((strategy_config.index_options_scanner or {}).get("enabled", False)),
    )
    store.ensure_default_strategy_settings(context)
    if not args.no_migrate_state:
        summary = store.migrate_json_state(context, state_path)
    else:
        summary = store.summary(context)

    output: dict[str, Any] = {
        "ok": True,
        "db_path": summary["db_path"],
        "user_email": summary["user_email"],
        "paper_account_name": summary["paper_account_name"],
        "state_saved_at": summary["state_saved_at"],
        "open_trades": summary["open_trades"],
        "closed_trades": summary["closed_trades"],
        "strategy_settings": summary["strategy_settings"],
        "json_state_exists": state_path.exists(),
    }
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
