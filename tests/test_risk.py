import unittest

from algotrader.config import RiskConfig
from algotrader.risk import RiskManager


class TestRiskSizing(unittest.TestCase):
    def test_position_size_respects_trade_value_cap(self) -> None:
        manager = RiskManager(
            RiskConfig(
                risk_per_trade_pct=1.0,
                max_open_positions=3,
                max_daily_loss=3000,
                reward_to_risk=2.0,
                sl_atr_multiplier=1.5,
                max_trade_value=50000,
            ),
            capital=100000,
        )

        quantity = manager.position_size(entry_price=1000, stop_loss=999, available_capital=100000)

        self.assertEqual(quantity, 50)

    def test_position_size_respects_available_balance(self) -> None:
        manager = RiskManager(
            RiskConfig(
                risk_per_trade_pct=1.0,
                max_open_positions=3,
                max_daily_loss=3000,
                reward_to_risk=2.0,
                sl_atr_multiplier=1.5,
                max_trade_value=50000,
            ),
            capital=100000,
        )

        quantity = manager.position_size(entry_price=500, stop_loss=499, available_capital=12000)

        self.assertEqual(quantity, 24)

    def test_position_size_respects_option_lot_size(self) -> None:
        manager = RiskManager(
            RiskConfig(
                risk_per_trade_pct=1.0,
                max_open_positions=3,
                max_daily_loss=3000,
                reward_to_risk=2.0,
                sl_atr_multiplier=1.5,
                max_trade_value=50000,
            ),
            capital=100000,
        )

        quantity = manager.position_size(
            entry_price=100,
            stop_loss=95,
            available_capital=50000,
            lot_size=75,
        )

        self.assertEqual(quantity, 150)

    def test_new_trade_block_reason_names_var_gate(self) -> None:
        manager = RiskManager(
            RiskConfig(
                risk_per_trade_pct=1.0,
                max_open_positions=3,
                max_daily_loss=3000,
                reward_to_risk=2.0,
                sl_atr_multiplier=1.5,
                max_var_pct=2.0,
            ),
            capital=100000,
        )
        manager.last_var_pct = 5.41

        reason = manager.new_trade_block_reason(open_trade_count=0)

        self.assertEqual(reason, "VaR 5.41% exceeds max_var_pct 2.00%")

    def test_zero_portfolio_drawdown_limit_disables_gate(self) -> None:
        manager = RiskManager(
            RiskConfig(
                risk_per_trade_pct=1.0,
                max_open_positions=3,
                max_daily_loss=3000,
                reward_to_risk=2.0,
                sl_atr_multiplier=1.5,
                max_portfolio_drawdown_pct=0.0,
            ),
            capital=100000,
        )
        manager.peak_equity = 120000
        manager.realized_pnl = -20000

        reason = manager.new_trade_block_reason(open_trade_count=0, realized_pnl=0.0)

        self.assertEqual(reason, "")

    def test_positive_portfolio_drawdown_limit_still_blocks(self) -> None:
        manager = RiskManager(
            RiskConfig(
                risk_per_trade_pct=1.0,
                max_open_positions=3,
                max_daily_loss=3000,
                reward_to_risk=2.0,
                sl_atr_multiplier=1.5,
                max_portfolio_drawdown_pct=10.0,
            ),
            capital=100000,
        )
        manager.peak_equity = 120000
        manager.realized_pnl = -20000

        reason = manager.new_trade_block_reason(open_trade_count=0, realized_pnl=0.0)

        self.assertEqual(
            reason,
            "Drawdown 33.33% exceeds max_portfolio_drawdown_pct 10.00%",
        )


if __name__ == "__main__":
    unittest.main()
