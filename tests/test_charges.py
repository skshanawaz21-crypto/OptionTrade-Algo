import unittest

from algotrader.charges import equity_intraday_charges, option_buy_charges


class TestCharges(unittest.TestCase):
    def test_equity_intraday_charges_are_positive(self) -> None:
        charges = equity_intraday_charges(
            buy_price=100.0,
            sell_price=101.0,
            quantity=100,
            exchange="NSE",
        )

        self.assertGreater(charges.brokerage, 0.0)
        self.assertGreater(charges.stt, 0.0)
        self.assertGreater(charges.exchange_txn, 0.0)
        self.assertGreater(charges.sebi, 0.0)
        self.assertGreater(charges.gst, 0.0)
        self.assertGreater(charges.stamp_duty, 0.0)
        self.assertGreater(charges.total, 0.0)

    def test_option_buy_charges_are_positive(self) -> None:
        charges = option_buy_charges(
            buy_price=120.0,
            sell_price=135.0,
            quantity=75,
            exchange="NFO",
        )

        self.assertGreater(charges.brokerage, 0.0)
        self.assertGreater(charges.stt, 0.0)
        self.assertGreater(charges.exchange_txn, 0.0)
        self.assertGreater(charges.sebi, 0.0)
        self.assertGreater(charges.gst, 0.0)
        self.assertGreater(charges.stamp_duty, 0.0)
        self.assertGreater(charges.total, 0.0)


if __name__ == "__main__":
    unittest.main()
