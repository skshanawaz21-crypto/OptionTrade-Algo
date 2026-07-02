import unittest

from algotrader.options import black_scholes_greeks, implied_volatility


class TestOptionsMath(unittest.TestCase):
    def test_call_pricing_outputs_expected_ranges(self) -> None:
        greeks = black_scholes_greeks(
            spot_price=100,
            strike_price=100,
            time_to_expiry_years=1.0,
            risk_free_rate=0.05,
            volatility=0.2,
            option_type="call",
        )
        self.assertAlmostEqual(greeks["price"], 10.45, delta=0.05)
        self.assertAlmostEqual(greeks["delta"], 0.636, delta=0.01)
        self.assertAlmostEqual(greeks["gamma"], 0.018, delta=0.002)

    def test_implied_volatility_recovers_market_assumption(self) -> None:
        iv = implied_volatility(
            market_price=10.45,
            spot_price=100,
            strike_price=100,
            time_to_expiry_years=1.0,
            risk_free_rate=0.05,
            option_type="call",
        )
        self.assertIsNotNone(iv)
        self.assertAlmostEqual(iv, 0.2, delta=0.01)


if __name__ == "__main__":
    unittest.main()
