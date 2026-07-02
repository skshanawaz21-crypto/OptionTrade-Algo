import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from algotrader.engine import TradingEngine
from algotrader.models import OpenTrade, Signal


class BrokerStub:
    def __init__(self, price: float) -> None:
        self.price = price

    def get_ltp(self, exchange: str, symbol: str) -> float:
        return self.price


class LoggerStub:
    def info(self, *args, **kwargs) -> None:
        return None

    def warning(self, *args, **kwargs) -> None:
        return None


class TestEngineExits(unittest.TestCase):
    def _risk_namespace(self, **overrides):
        defaults = {
            "min_option_premium": 0.0,
            "max_premium_per_trade": 0.0,
            "max_exposure_per_underlying": 0.0,
            "max_spread_pct": 0.0,
            "max_slippage_pct": 0.0,
            "strategy_daily_loss_limit": 0.0,
            "same_symbol_cooldown_minutes": 0,
            "same_side_loss_cooldown_minutes": 0,
            "same_side_loss_cooldown_count": 0,
            "square_off_on_daily_loss": False,
            "max_daily_loss": 10000.0,
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_scanner_trade_is_managed_even_when_symbol_is_not_in_watchlist(self) -> None:
        engine = TradingEngine.__new__(TradingEngine)
        trade = OpenTrade(
            symbol="KPITTECH",
            exchange="NFO",
            direction="BUY",
            tradingsymbol="KPITTECH26MAY760CE",
            entry_price=28.30,
            stop_loss=22.64,
            target_price=39.62,
            quantity=425,
            instrument_type="stock_option",
            underlying_symbol="KPITTECH",
            underlying_exchange="NSE",
            lot_size=425,
            lots=1,
            option_side="CE",
            option_expiry="2026-05-26",
            option_strike=760.0,
        )
        engine.open_trades = {"KPITTECH:stock_option": trade}
        engine.broker = BrokerStub(price=47.05)
        engine.logger = LoggerStub()
        engine._last_open_trade_check = None

        closed = []

        def close_trade(closed_trade, price: float, reason: str) -> None:
            closed.append((closed_trade.tradingsymbol, price, reason))
            engine.open_trades.pop("KPITTECH:stock_option", None)

        engine._close_trade = close_trade

        TradingEngine._manage_all_open_trades(engine, force=True)

        self.assertEqual(closed, [("KPITTECH26MAY760CE", 47.05, "Target hit")])
        self.assertEqual(engine.open_trades, {})

    def test_session_square_off_closes_open_trade_after_cutoff(self) -> None:
        engine = TradingEngine.__new__(TradingEngine)
        trade = OpenTrade(
            symbol="PATANJALI",
            exchange="NFO",
            direction="BUY",
            tradingsymbol="PATANJALI26MAY460CE",
            entry_price=12.70,
            stop_loss=10.16,
            target_price=17.78,
            quantity=900,
            instrument_type="stock_option",
            underlying_symbol="PATANJALI",
            underlying_exchange="NSE",
            lot_size=900,
            lots=1,
            option_side="CE",
            option_expiry="2026-05-26",
            option_strike=460.0,
        )
        engine.open_trades = {"PATANJALI:stock_option": trade}
        engine.broker = BrokerStub(price=13.35)
        engine.logger = LoggerStub()
        engine.strategy_config = SimpleNamespace(
            session_rules=SimpleNamespace(
                timezone="Asia/Kolkata",
                square_off_open_positions=True,
                square_off_time="00:00",
            )
        )
        closed = []

        def close_trade(closed_trade, price: float, reason: str) -> None:
            closed.append((closed_trade.tradingsymbol, price, reason))
            engine.open_trades.pop("PATANJALI:stock_option", None)

        engine._close_trade = close_trade

        TradingEngine._square_off_open_trades_if_due(engine)

        self.assertEqual(closed, [("PATANJALI26MAY460CE", 13.35, "Session square-off")])
        self.assertEqual(engine.open_trades, {})

    def test_profit_protection_moves_stop_to_breakeven_after_30_percent_progress(self) -> None:
        engine = TradingEngine.__new__(TradingEngine)
        trade = OpenTrade(
            symbol="TCS",
            exchange="NFO",
            direction="BUY",
            tradingsymbol="TCS26MAY2300PE",
            entry_price=69.40,
            stop_loss=55.52,
            target_price=97.16,
            quantity=175,
            instrument_type="stock_option",
            underlying_symbol="TCS",
            underlying_exchange="NSE",
            initial_stop_loss=55.52,
            initial_target_price=97.16,
            max_favourable_price=69.40,
        )
        engine.strategy_config = SimpleNamespace(
            risk=SimpleNamespace(
                profit_protection_enabled=True,
                breakeven_trigger_pct=30.0,
                breakeven_buffer_pct=0.5,
                lock_profit_trigger_pct=50.0,
                lock_profit_pct=25.0,
                target_extension_trigger_pct=70.0,
                target_extension_pct=10.0,
                target_extension_stop_lock_pct=50.0,
                max_target_extensions=2,
            )
        )
        engine.logger = LoggerStub()
        saved = []
        engine._save_state = lambda: saved.append(True)

        TradingEngine._apply_profit_protection(engine, trade, price=80.50)

        self.assertEqual(trade.stop_loss, 69.75)
        self.assertEqual(trade.target_price, 97.16)
        self.assertEqual(saved, [True])

    def test_new_position_gate_uses_today_realized_pnl_for_daily_loss(self) -> None:
        engine = TradingEngine.__new__(TradingEngine)
        ist = ZoneInfo("Asia/Kolkata")
        engine.open_trades = {}
        engine.closed_trades = [
            SimpleNamespace(pnl=20000.0, closed_at=datetime(2026, 5, 19, 15, 0, tzinfo=ist)),
            SimpleNamespace(pnl=-6000.0, closed_at=datetime(2026, 5, 20, 10, 0, tzinfo=ist)),
            SimpleNamespace(pnl=-4500.0, closed_at=datetime(2026, 5, 20, 11, 0, tzinfo=ist)),
        ]
        engine.risk_manager = SimpleNamespace(
            new_trade_block_reason=lambda count, realized_pnl=None: (
                "Daily loss 10500.00 reached max_daily_loss 10000.00"
                if realized_pnl is not None and realized_pnl <= -10000
                else ""
            )
        )
        engine.strategy_config = SimpleNamespace(
            risk=SimpleNamespace(max_stock_option_positions=2, max_index_option_positions=2),
            index_options_scanner={},
        )

        self.assertEqual(
            TradingEngine._daily_realized_pnl(engine, datetime(2026, 5, 20, tzinfo=ist).date()),
            -10500.0,
        )
        engine._daily_realized_pnl = lambda: -10500.0
        self.assertIn(
            "Daily loss 10500.00",
            TradingEngine._new_position_block_reason(engine, "stock_option"),
        )

    def test_entry_risk_gate_blocks_tiny_option_premium(self) -> None:
        engine = TradingEngine.__new__(TradingEngine)
        engine.strategy_config = SimpleNamespace(
            risk=self._risk_namespace(min_option_premium=5.0),
            contract_selection=SimpleNamespace(max_spread_pct=0.0),
        )
        engine.open_trades = {}
        engine.closed_trades = []
        signal = Signal(
            symbol="YESBANK",
            exchange="NFO",
            direction="BUY",
            regime="bullish",
            entry_price=0.14,
            stop_loss=0.11,
            target_price=0.20,
            quantity=62200,
            reason="NIFTY250_2m_scanner score=61.11 vol_ratio=0.76",
            tradingsymbol="YESBANK26MAY22CE",
            instrument_type="stock_option",
            option_side="CE",
        )

        reason = TradingEngine._entry_risk_gate_reason(engine, signal)

        self.assertIn("min_option_premium", reason)

    def test_entry_risk_gate_blocks_strategy_after_daily_loss(self) -> None:
        engine = TradingEngine.__new__(TradingEngine)
        ist = ZoneInfo("Asia/Kolkata")
        engine.strategy_config = SimpleNamespace(
            risk=self._risk_namespace(strategy_daily_loss_limit=5000.0),
            contract_selection=SimpleNamespace(max_spread_pct=0.0),
        )
        engine.open_trades = {}
        engine.closed_trades = [
            SimpleNamespace(
                symbol="SENSEX",
                instrument_type="index_option",
                entry_reason="index_options_scanner interval=5minute score=77.00",
                tradingsymbol="SENSEX2652175400CE",
                option_side="CE",
                pnl=-5200.0,
                closed_at=datetime.now(ist),
            )
        ]
        signal = Signal(
            symbol="NIFTY",
            exchange="NFO",
            direction="BUY",
            regime="bullish",
            entry_price=120.0,
            stop_loss=96.0,
            target_price=168.0,
            quantity=50,
            reason="index_options_scanner interval=5minute score=80.00",
            tradingsymbol="NIFTY26MAY24000CE",
            instrument_type="index_option",
            option_side="CE",
        )

        reason = TradingEngine._entry_risk_gate_reason(engine, signal)

        self.assertIn("Index Options Scanner daily loss", reason)

    def test_entry_risk_gate_blocks_repeated_same_side_losses(self) -> None:
        engine = TradingEngine.__new__(TradingEngine)
        ist = ZoneInfo("Asia/Kolkata")
        engine.strategy_config = SimpleNamespace(
            risk=self._risk_namespace(
                same_side_loss_cooldown_minutes=45,
                same_side_loss_cooldown_count=2,
            ),
            contract_selection=SimpleNamespace(max_spread_pct=0.0),
        )
        engine.open_trades = {}
        engine.closed_trades = [
            SimpleNamespace(
                symbol="LTF",
                instrument_type="stock_option",
                entry_reason="NIFTY250_2m_scanner score=79.87 vol_ratio=2.97",
                tradingsymbol="LTF26MAY265PE",
                option_side="PE",
                pnl=-1000.0,
                closed_at=datetime.now(ist),
            ),
            SimpleNamespace(
                symbol="NESTLEIND",
                instrument_type="stock_option",
                entry_reason="NIFTY250_2m_scanner score=60.28 vol_ratio=0.61",
                tradingsymbol="NESTLEIND26MAY1420PE",
                option_side="PE",
                pnl=-1200.0,
                closed_at=datetime.now(ist),
            ),
        ]
        signal = Signal(
            symbol="AXISBANK",
            exchange="NFO",
            direction="BUY",
            regime="bearish",
            entry_price=8.0,
            stop_loss=6.4,
            target_price=11.2,
            quantity=625,
            reason="NIFTY250_2m_scanner score=66.00 vol_ratio=2.00",
            tradingsymbol="AXISBANK26MAY1290PE",
            instrument_type="stock_option",
            option_side="PE",
        )

        reason = TradingEngine._entry_risk_gate_reason(engine, signal)

        self.assertIn("PE entries cooling down", reason)

    def test_daily_loss_kill_switch_flattens_open_trades(self) -> None:
        engine = TradingEngine.__new__(TradingEngine)
        trade = OpenTrade(
            symbol="TCS",
            exchange="NFO",
            direction="BUY",
            tradingsymbol="TCS26MAY2300PE",
            entry_price=100.0,
            stop_loss=80.0,
            target_price=140.0,
            quantity=20,
            instrument_type="stock_option",
            current_price=40.0,
        )
        engine.open_trades = {"TCS:stock_option": trade}
        engine.closed_trades = []
        engine.strategy_config = SimpleNamespace(
            risk=self._risk_namespace(
                square_off_on_daily_loss=True,
                max_daily_loss=1000.0,
            )
        )
        engine.broker = BrokerStub(price=39.0)
        engine.logger = LoggerStub()
        closed = []

        def close_trade(closed_trade, price: float, reason: str) -> None:
            closed.append((closed_trade.tradingsymbol, price, reason))
            engine.open_trades.pop("TCS:stock_option", None)

        engine._close_trade = close_trade

        triggered = TradingEngine._daily_loss_kill_switch_if_needed(engine)

        self.assertTrue(triggered)
        self.assertEqual(closed, [("TCS26MAY2300PE", 39.0, "Daily loss kill switch")])


if __name__ == "__main__":
    unittest.main()
