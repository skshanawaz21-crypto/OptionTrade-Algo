import unittest

from algotrader.models import OpenTrade, OptionMetrics


class TestOpenTradeSerialization(unittest.TestCase):
    def test_open_trade_round_trip(self) -> None:
        trade = OpenTrade(
            symbol="SBIN",
            exchange="NSE",
            direction="BUY",
            tradingsymbol="SBIN",
            entry_price=100.5,
            stop_loss=98.0,
            target_price=105.0,
            quantity=10,
            instrument_type="index_option",
            underlying_symbol="NIFTY",
            underlying_exchange="NSE",
            lot_size=75,
            lots=1,
            option_side="CE",
            option_expiry="2026-04-30",
            option_strike=24000.0,
            option_metrics=OptionMetrics(delta=0.45, implied_volatility=0.22),
        )

        restored = OpenTrade.from_dict(trade.to_dict())

        self.assertEqual(restored.symbol, "SBIN")
        self.assertEqual(restored.direction, "BUY")
        self.assertEqual(restored.quantity, 10)
        self.assertEqual(restored.option_side, "CE")
        self.assertEqual(restored.lot_size, 75)
        self.assertIsNotNone(restored.option_metrics)
        self.assertAlmostEqual(restored.option_metrics.delta or 0.0, 0.45, places=6)


if __name__ == "__main__":
    unittest.main()
