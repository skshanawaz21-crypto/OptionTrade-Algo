import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from algotrader.config import ContractSelectionConfig
from algotrader.engine import TradingEngine
from algotrader.marketdata import completed_intraday_candles
from algotrader.models import OpenTrade


class TestIndexOptionsScanner(unittest.TestCase):
    def test_incomplete_five_minute_candle_is_not_analyzed(self) -> None:
        candles = pd.DataFrame(
            {
                "date": [
                    "2026-06-10 09:40:00",
                    "2026-06-10 09:45:00",
                ],
                "open": [100.0, 101.0],
                "high": [102.0, 104.0],
                "low": [99.0, 100.0],
                "close": [101.0, 103.0],
                "volume": [1000, 1200],
            }
        )

        filtered = completed_intraday_candles(
            candles,
            "5minute",
            datetime(2026, 6, 10, 9, 48, tzinfo=ZoneInfo("Asia/Kolkata")),
        )

        self.assertEqual(len(filtered), 1)
        self.assertEqual(str(filtered.iloc[-1]["date"]), "2026-06-10 09:40:00")

    def test_five_minute_candle_is_available_after_close(self) -> None:
        candles = pd.DataFrame(
            {
                "date": ["2026-06-10 09:45:00"],
                "open": [101.0],
                "high": [104.0],
                "low": [100.0],
                "close": [103.0],
                "volume": [1200],
            }
        )

        filtered = completed_intraday_candles(
            candles,
            "5minute",
            datetime(2026, 6, 10, 9, 50, tzinfo=ZoneInfo("Asia/Kolkata")),
        )

        self.assertEqual(len(filtered), 1)

    def test_sensex_uses_bse_underlying_and_bfo_contracts(self) -> None:
        engine = TradingEngine.__new__(TradingEngine)
        engine.strategy_config = SimpleNamespace(
            contract_selection=SimpleNamespace(max_dte=14),
        )

        item = TradingEngine._index_option_watch_item(
            engine,
            "SENSEX",
            "5minute",
            {"contract_exchange_by_symbol": {"SENSEX": "BFO"}},
        )

        self.assertEqual(item.symbol, "SENSEX")
        self.assertEqual(item.exchange, "BSE")
        self.assertEqual(item.contract_exchange, "BFO")
        self.assertEqual(item.instrument_type, "index_option")

    def test_bullish_index_snapshot_creates_ce_decision(self) -> None:
        engine = TradingEngine.__new__(TradingEngine)
        snapshot = SimpleNamespace(
            close=24000.0,
            ema20=24050.0,
            ema50=23900.0,
            rsi14=62.0,
            momentum=0.0006,
            breakout_up=False,
            breakout_down=False,
            regime="bullish",
        )

        decision = TradingEngine._index_scanner_decision(
            engine,
            snapshot,
            {
                "min_score": 60,
                "require_breakout": False,
                "bullish_rsi": 55,
                "bearish_rsi": 45,
                "min_momentum_pct": 0.02,
                "min_ema_gap_pct": 0.03,
            },
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision["direction"], "BULLISH")
        self.assertEqual(decision["option_side"], "CE")

    def test_bearish_index_snapshot_creates_pe_decision(self) -> None:
        engine = TradingEngine.__new__(TradingEngine)
        snapshot = SimpleNamespace(
            close=54000.0,
            ema20=53800.0,
            ema50=54100.0,
            rsi14=38.0,
            momentum=-0.0007,
            breakout_up=False,
            breakout_down=False,
            regime="bearish",
        )

        decision = TradingEngine._index_scanner_decision(
            engine,
            snapshot,
            {
                "min_score": 60,
                "require_breakout": False,
                "bullish_rsi": 55,
                "bearish_rsi": 45,
                "min_momentum_pct": 0.02,
                "min_ema_gap_pct": 0.03,
            },
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision["direction"], "BEARISH")
        self.assertEqual(decision["option_side"], "PE")

    def test_banknifty_index_scanner_can_override_to_monthly_expiry(self) -> None:
        engine = TradingEngine.__new__(TradingEngine)
        engine.strategy_config = SimpleNamespace(
            contract_selection=ContractSelectionConfig(
                expiry_type="weekly",
                min_dte=1,
                max_dte=14,
            )
        )

        policy = TradingEngine._index_scanner_contract_policy(
            engine,
            "BANKNIFTY",
            {
                "expiry_type_by_symbol": {"BANKNIFTY": "monthly"},
                "max_dte_by_symbol": {"BANKNIFTY": 45},
            },
        )

        self.assertEqual(policy.expiry_type, "monthly")
        self.assertEqual(policy.max_dte, 45)
        self.assertEqual(policy.min_dte, 1)

    def test_index_option_position_pool_limit_blocks_only_index_entries(self) -> None:
        engine = TradingEngine.__new__(TradingEngine)
        engine.risk_manager = SimpleNamespace(new_trade_block_reason=lambda count, realized_pnl=None: "")
        engine.strategy_config = SimpleNamespace(
            risk=SimpleNamespace(max_stock_option_positions=2, max_index_option_positions=2),
            index_options_scanner={},
        )
        engine.open_trades = {
            "NIFTY:index_option": OpenTrade(
                symbol="NIFTY",
                exchange="NFO",
                direction="BUY",
                tradingsymbol="NIFTY26MAY24000CE",
                entry_price=100.0,
                stop_loss=80.0,
                target_price=140.0,
                quantity=50,
                instrument_type="index_option",
            ),
            "BANKNIFTY:index_option": OpenTrade(
                symbol="BANKNIFTY",
                exchange="NFO",
                direction="BUY",
                tradingsymbol="BANKNIFTY26MAY54000CE",
                entry_price=100.0,
                stop_loss=80.0,
                target_price=140.0,
                quantity=30,
                instrument_type="index_option",
            ),
        }

        blocked = TradingEngine._new_position_block_reason(engine, "index_option")
        self.assertIn("max_index_option_positions 2", blocked)

    def test_stock_option_position_pool_limit_blocks_only_stock_entries(self) -> None:
        engine = TradingEngine.__new__(TradingEngine)
        engine.risk_manager = SimpleNamespace(new_trade_block_reason=lambda count, realized_pnl=None: "")
        engine.strategy_config = SimpleNamespace(
            risk=SimpleNamespace(max_stock_option_positions=2, max_index_option_positions=2),
            index_options_scanner={},
        )
        engine.open_trades = {
            "TCS:stock_option": OpenTrade(
                symbol="TCS",
                exchange="NFO",
                direction="BUY",
                tradingsymbol="TCS26MAY3600CE",
                entry_price=120.0,
                stop_loss=96.0,
                target_price=168.0,
                quantity=75,
                instrument_type="stock_option",
            ),
            "RELIANCE:stock_option": OpenTrade(
                symbol="RELIANCE",
                exchange="NFO",
                direction="BUY",
                tradingsymbol="RELIANCE26MAY2900CE",
                entry_price=90.0,
                stop_loss=72.0,
                target_price=126.0,
                quantity=250,
                instrument_type="stock_option",
            ),
        }

        blocked = TradingEngine._new_position_block_reason(engine, "stock_option")
        self.assertIn("max_stock_option_positions 2", blocked)


if __name__ == "__main__":
    unittest.main()
